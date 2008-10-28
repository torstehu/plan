# encoding: utf-8

import logging

from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template.context import RequestContext
from django.template.defaultfilters import slugify
from django.core.cache import cache
from django.views.generic.list_detail import object_list
from django.conf import settings

from plan.common.models import Course, Deadline, Exam, Group, \
        Lecture, Semester, UserSet, Room, Lecturer, Week
from plan.common.forms import DeadlineForm, GroupForm, CourseNameForm
from plan.common.utils import compact_sequence, ColorMap
from plan.common.timetable import Timetable

def clear_cache(*args):
    """Clears a users cache based on reverse"""

    cache.delete('stats')
    cache.delete(reverse('schedule', args=args))
    cache.delete(reverse('schedule-advanced', args=args))
    cache.delete(reverse('schedule-ical', args=args))
    cache.delete(reverse('schedule-ical-exams', args=args))
    cache.delete(reverse('schedule-ical-lectures', args=args))
    cache.delete(reverse('schedule-ical-deadlines', args=args))

    logging.debug('Deleted cache')

def shortcut(request, slug):
    '''Redirect users to their timetable for the current semester'''

    semester = Semester.current()

    return HttpResponseRedirect(reverse('schedule',
            args = [semester.year, semester.get_type_display(), slug.strip()]))

def getting_started(request):
    '''Intial top level page that greets users'''

    semester = Semester.current()

    # Redirect user to their timetable
    if request.method == 'POST' and 'slug' in request.POST:
        slug = slugify(request.POST['slug'])

        if slug.strip():
            response = HttpResponseRedirect(reverse('schedule',
                    args = [semester.year, semester.get_type_display(), slug]))

            # Store last timetable visited in a cookie so that we can populate
            # the field with a default value next time.
            response.set_cookie('last', slug, 60*60*24*7*4)
            return response

    context = cache.get('stats')

    if not context or 'no-cache' in request.GET:
        slug_count = int(UserSet.objects.values('slug').distinct().count())
        subscription_count = int(UserSet.objects.count())
        deadline_count = int(Deadline.objects.count())

        context = {
            'color_map': ColorMap(),
            'slug_count': slug_count,
            'subscription_count': subscription_count,
            'deadline_count': deadline_count,
            'stats': Course.get_stats(),

        }

        cache.set('stats', context)

    return render_to_response('start.html', context, RequestContext(request))

def schedule(request, year, semester_type, slug, advanced=False,
        week=None, deadline_form=None, cache_page=True):
    '''Page that handels showing schedules'''

    response = cache.get(request.path)

    if response and 'no-cache' not in request.GET and cache_page:
        return response

    # Color mapping for the courses
    color_map = ColorMap()

    group_forms = {}

    # Keep track if all groups are selected for all courses
    all_groups = False

    semester = Semester(year=year, type=semester_type)

    # Start setting up queries
    courses = Course.objects.get_courses(year, semester.type, slug)

    deadlines = Deadline.objects.get_deadlines(year, semester.type, slug)
    lectures = Lecture.objects.get_lectures(year, semester.type, slug)
    exams = Exam.objects.get_exams(year, semester.type, slug)

    # Use get_related to cut query counts
    lecturers = Lecture.get_related(Lecturer, lectures)
    groups = Lecture.get_related(Group, lectures)
    rooms = Lecture.get_related(Room, lectures)
    weeks = Lecture.get_related(Week, lectures, field='number')

    # Init colors in predictable maner
    for c in courses:
        color_map[c.id]

    # Create Timetable
    table = Timetable(lectures, rooms)
    if lectures:
        table.place_lectures()
        table.do_expansion()
    table.insert_times()

    # Add extra info to lectures
    for lecture in lectures:
        compact_weeks = compact_sequence(weeks.get(lecture.id, []))

        lecture.sql_weeks = compact_weeks
        lecture.sql_groups = groups.get(lecture.id, [])
        lecture.sql_lecturers = lecturers.get(lecture.id, [])
        lecture.sql_rooms = rooms.get(lecture.id, [])

    if advanced:
        usersets = UserSet.objects.get_usersets(year, semester.type, slug)

        # Set up deadline form
        if not deadline_form:
            deadline_form = DeadlineForm(usersets)

        if courses:
            course_groups = Course.get_groups([u.course_id for u in usersets])
            selected_groups = UserSet.get_groups(year, semester.type, slug)

            for u in usersets:
                if not all_groups and len(course_groups[u.course_id]) > 3:
                    all_groups = set(selected_groups[u.id]) == \
                            set(map(lambda a: a[0], course_groups[u.course_id]))

                group_forms[u.course_id] = GroupForm(course_groups[u.course_id],
                        initial={'groups': selected_groups.get(u.id, [])},
                        prefix=u.course_id)

        # Set up group forms
        for course in courses:
            course.group_form = group_forms.get(course.id, None)

            name = course.alias or ''
            course.name_form = CourseNameForm(initial={'name': name},
                     prefix=course.id)

    response = render_to_response('schedule.html', {
            'advanced': advanced,
            'color_map': color_map,
            'courses': courses,
            'deadline_form': deadline_form,
            'deadlines': deadlines,
            'exams': exams,
            'group_help': all_groups,
            'lectures': lectures,
            'semester': semester,
            'slug': slug,
            'timetable': table,
        }, RequestContext(request))

    if cache_page:
        if deadlines:
            cache_time = deadlines[0].get_seconds()
        else:
            cache_time = settings.CACHE_TIME

        cache.set(request.path, response, cache_time)
    return response

def select_groups(request, year, semester_type, slug):
    '''Form handler for selecting groups to use in schedule'''

    semester = Semester(year=year, type=semester_type)

    if request.method == 'POST':
        courses = Course.objects.get_courses(year, semester.type, slug)

        course_groups = Course.get_groups([c.id for c in courses])

        for c in courses:
            groups = course_groups[c.id]
            group_form = GroupForm(groups, request.POST, prefix=c.id)

            if group_form.is_valid():
                userset = UserSet.objects.get_usersets(year,
                        semester.type, slug).get(course=c)

                userset.groups = group_form.cleaned_data['groups']

        clear_cache(year, semester.get_type_display(), slug)

    return HttpResponseRedirect(reverse('schedule-advanced',
            args=[semester.year,semester.get_type_display(),slug]))

def new_deadline(request, year, semester_type, slug):
    '''Handels addition of tasks, reshows schedule view if form does not
       validate'''
    semester = Semester(year=year, type=semester_type)

    if request.method == 'POST':
        clear_cache(year, semester.get_type_display(), slug)

        post = request.POST.copy()

        if 'submit_add' in post and 'submit_remove' in post:
            # IE6 doesn't handle <button> correctly, it submits all buttons
            if 'deadline_remove' in post:
                # User has checked at least on deadline to remove, make a blind
                # guess and remove submit_add button.
                del post['submit_add']

        if 'submit_add' in post:
            usersets = UserSet.objects.get_usersets(year, semester.type, slug)
            deadline_form = DeadlineForm(usersets, post)

            if deadline_form.is_valid():
                deadline_form.save()
            else:
                return schedule(request, year, semester_type, slug, advanced=True,
                        deadline_form=deadline_form, cache_page=False)

        elif 'submit_remove' in post:
            logging.debug(post.getlist('deadline_remove'))
            Deadline.objects.get_deadlines(year, semester.type, slug).filter(
                    id__in=post.getlist('deadline_remove')
                ).delete()

    return HttpResponseRedirect(reverse('schedule-advanced',
            args = [semester.year,semester.get_type_display(),slug]))

def copy_deadlines(request, year, semester_type, slug):
    '''Handles importing of deadlines'''

    semester = Semester(year=year, type=semester_type)

    if request.method == 'POST':
        if 'slugs' in request.POST:
            slugs = request.POST['slugs'].replace(',', ' ').split()

            color_map = ColorMap()

            courses = Course.objects.get_courses(year, semester.type, slug). \
                    distinct()

            # Init color map
            for c in courses:
                color_map[c.id]

            deadlines = Deadline.objects.filter(
                    userset__slug__in=slugs,
                    userset__semester__year__exact=year,
                    userset__semester__type__exact=semester.type,
                    userset__course__in=courses,
                ).select_related(
                    'userset__course__id'
                ).exclude(userset__slug=slug)

            return render_to_response('select_deadlines.html', {
                    'color_map': color_map,
                    'deadlines': deadlines,
                    'semester': semester,
                    'slug': slug,
                }, RequestContext(request))

        elif 'deadline_id' in request.POST:
            deadline_ids = request.POST.getlist('deadline_id')
            deadlines = Deadline.objects.filter(
                    id__in=deadline_ids,
                    userset__semester__year__exact=year,
                    userset__semester__type__exact=semester.type,
                )

            for d in deadlines:
                userset = UserSet.objects.get_usersets(year, semester.type,
                    slug).get(course=d.userset.course)

                Deadline.objects.get_or_create(
                        userset=userset,
                        date=d.date,
                        time=d.time,
                        task=d.task
                )
            clear_cache(year, semester.get_type_display(), slug)

    return HttpResponseRedirect(reverse('schedule',
            args=[semester.year,semester.get_type_display(),slug]))

def select_course(request, year, semester_type, slug, add=False):
    '''Handle selecting of courses from course list, change of names and
       removeall of courses'''

    # FIXME split ut three sub functions into seperate functions?

    semester = Semester(type=semester_type)
    semester = Semester.objects.get(year=year, type=semester.type)

    if request.method == 'POST':

        clear_cache(year, semester.get_type_display(), slug)

        post = request.POST.copy()

        if 'submit_add' in post and 'submit_remove' in post and \
                'submit_name' in post:
            # IE6 doesn't handle <button> correctly, it submits all buttons
            if 'course_remove' in post:
                # User has checked at least on course to remove, make a blind
                # guess and remove submit_add button.
                del post['submit_add']
                del post['submit_name']
            else:
                if post['course_add'].strip():
                    # Someone put something in course add box, assumme thats
                    # what they want to do
                    del post['submit_remove']
                    del post['submit_name']
                else:
                    del post['submit_remove']
                    del post['submit_add']

        if 'submit_add' in post or add:
            lookup = []

            for l in post.getlist('course_add'):
                lookup.extend(l.replace(',', '').split())

            errors = []

            # FIXME limit max courses to for instance 30

            for l in lookup:
                try:
                    course = Course.objects.get(
                            name__iexact=l.strip(),
                            semesters__in=[semester],
                        )
                    userset, created = UserSet.objects.get_or_create(
                            slug=slug,
                            course=course,
                            semester=semester
                        )

                    groups = Group.objects.filter(
                            lecture__course=course
                        ).distinct()
                    for g in groups:
                        userset.groups.add(g)

                except Course.DoesNotExist:
                    errors.append(l)

            if errors:
                return render_to_response('error.html', {
                        'courses': errors,
                        'slug': slug,
                        'year': year,
                        'type': semester.get_type_display()
                    }, RequestContext(request))

        elif 'submit_remove' in post:
            courses = []
            for c in post.getlist('course_remove'):
                if c.strip():
                    courses.append(c.strip())

            UserSet.objects.get_usersets(year, semester.type, slug). \
                    filter(course__id__in=courses).delete()

        elif 'submit_name' in post:
            usersets = UserSet.objects.get_usersets(year, semester.type, slug)

            for u in usersets:
                form = CourseNameForm(post, prefix=u.course_id)

                if form.is_valid():
                    name = form.cleaned_data['name'].strip()

                    if name.upper() == u.course.name.upper() or name == "":
                        # Leave as blank if we match the current course name
                        name = ""

                    u.name = name
                    u.save()

    return HttpResponseRedirect(reverse('schedule-advanced',
            args=[semester.year, semester.get_type_display(), slug]))

def select_lectures(request, year, semester_type, slug):
    '''Handle selection of lectures to hide'''
    semester = Semester(year=year, type=semester_type)

    if request.method == 'POST':
        excludes = request.POST.getlist('exclude')

        usersets = UserSet.objects.get_usersets(year, semester.type, slug)

        for userset in usersets:
            userset.exclude = userset.course.lecture_set.filter(id__in=excludes)

        clear_cache(semester.year, semester.get_type_display(), slug)

    return HttpResponseRedirect(reverse('schedule-advanced',
            args=[semester.year, semester.get_type_display(), slug]))

def list_courses(request, year, semester_type, slug):
    '''Display a list of courses based on when exam is'''

    if request.method == 'POST':
        return select_course(request, year, semester_type, slug, add=True)

    response = cache.get('course_list')

    if not response:
        semester = Semester(year=year, type=semester_type)

        courses = Course.objects.get_courses_with_exams(year, semester.type,
            semester.get_first_day(), semester.get_last_day())

        response = object_list(request,
                courses,
                extra_context={'semester': semester},
                template_object_name='course',
                template_name='course_list.html')

        cache.set('course_list', response)

    return response
