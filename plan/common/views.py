# encoding: utf-8

import re
import logging
import vobject

from datetime import datetime, timedelta
from dateutil.rrule import *
from urllib import urlopen, quote as urlquote, URLopener
from BeautifulSoup import BeautifulSoup, NavigableString

from django.core.urlresolvers import reverse, NoReverseMatch
from django.db.models import Q
from django.http import HttpResponseRedirect, HttpResponse, Http404
from django.shortcuts import get_object_or_404, render_to_response
from django.template.context import RequestContext
from django.template.defaultfilters import slugify
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from django.db import connection

from plan.common.models import *
from plan.common.forms import *
from plan.common.utils import *

MAX_COLORS = 8

def getting_started(request):
    if request.method == 'POST' and 'slug' in request.POST:
        slug = slugify(request.POST['slug'])

        if slug.strip():
            # Default to current semester
            if datetime.now().month <= 6:
                semester = Semester(type=Semester.SPRING).get_type_display()
            else:
                semester = Semester(type=Semester.FALL).get_type_display()

            response = HttpResponseRedirect(reverse('schedule', args=[datetime.now().year, semester, slug]))

            # Store last timetable visited in a cookie so that we can populate
            # the field with a default value next time.
            response.set_cookie('last', slug, 60*60*24*7*4)
            return response
    return render_to_response('common/start.html', {}, RequestContext(request))

def schedule(request, year, semester, slug, advanced=False, week=None):
    t = request.timer
    cache_key = ':'.join(['schedule', year, semester, slug, str(advanced)])
    response = cache.get(cache_key)

    if response and 'no-cache' not in request.GET:
        t.tick('Done, returning cache')
        return response

    cursor = connection.cursor()

    # Data structure that stores what will become the html table
    table = [[[{}] for a in Lecture.DAYS] for b in Lecture.START]

    # Extra info used to get the table right
    lectures = []

    # ids of included lectures
    included = []

    # Color mapping for the courses
    color_map = {}
    color_index = 0

    # Header colspans
    span = [1] * 5

    # Array with courses to show
    courses = []
    group_forms = {}

    # Helper arrays to keep query count down
    groups = {}
    lecturers = {}
    weeks = {}

    # Default to current year
    if not year:
        year = datetime.now().year

    semester = dict(map(lambda x: (x[1],x[0]), Semester.TYPES))[semester.lower()]
    semester = Semester.objects.get(year=year, type=semester)

    # Get all lectures for userset during given period. To do this we need to
    # pull in a bunch of extra tables and manualy join them in the where
    # cluase. The first element in the custom where is the important one that
    # limits our results, the rest are simply meant for joining.

    # FIXME convert the first where clause to a select boolean, this would
    # probably allow use to use the same initial_lectures query in both cases.
    where=[
        'common_userset_groups.group_id = common_group.id',
        'common_userset_groups.userset_id = common_userset.id',
        'common_group.id = common_lecture_groups.group_id',
        'common_lecture_groups.lecture_id = common_lecture.id'
    ]
    tables=[
        'common_userset_groups',
        'common_group',
        'common_lecture_groups'
    ]

    filter = {
        'course__userset__slug': slug,
        'course__userset__semester': semester,
    }

    related = [
        'type__name',
        'room__name',
        'course__name',
    ]

    order = [
        'course__name',
        'day',
        'start_time',
        'type__name',
    ]

    initial_lectures = Lecture.objects.filter(**filter).distinct().select_related(*related).extra(where=where, tables=tables).order_by(*order)

    t.tick('Done intializing')

    if advanced:
        for u in UserSet.objects.filter(slug=slug, semester=semester):
            # SQL: this causes extra queries (can be worked around, subquery?)
            initial_groups = u.groups.values_list('id', flat=True)

            # SQL: this causes extra queries (hard to work around, probably not
            # worh it)
            course_groups = Group.objects.filter(lecture__course__id=u.course_id).distinct()

            # FIXME Force evaluation of queries so that we can trace them better in the logs, should be removed.
            [len(groups), len(initial_groups)]

            # SQL: For loop generates to quries per userset.
            group_forms[u.course_id] = GroupForm(course_groups, initial={'groups': initial_groups}, prefix=u.course_id)

        t.tick('Done creating groups forms')

        # Do three custom sql queries to prevent and explosion of sql queries
        # due to ORM.
        cursor.execute('''SELECT common_lecture_groups.lecture_id, common_group.name
                            FROM common_lecture_groups
                            INNER JOIN common_group
                                ON (common_group.id = common_lecture_groups.group_id)''')

        for lecture_id,name in cursor.fetchall():
            if lecture_id not in groups:
                groups[lecture_id] = []

            groups[lecture_id].append(name)
        t.tick('Done getting groups for lecture list')

        cursor.execute('''SELECT common_lecture_lecturers.lecture_id, common_lecturer.name
                            FROM common_lecture_lecturers
                            INNER JOIN common_lecturer
                                ON (common_lecturer.id = common_lecture_lecturers.lecturer_id)''')

        for lecture_id,name in cursor.fetchall():
            if lecture_id not in lecturers:
                lecturers[lecture_id] = []

            lecturers[lecture_id].append(name)

        t.tick('Done getting lecturers for lecture list')

        cursor.execute('''SELECT common_lecture_weeks.lecture_id, common_week.number
                            FROM common_lecture_weeks
                            INNER JOIN common_week
                                ON (common_week.id = common_lecture_weeks.week_id)''')

        for lecture_id,name in cursor.fetchall():
            if lecture_id not in weeks:
                weeks[lecture_id] = []

            weeks[lecture_id].append(name)

        t.tick('Done getting weeks for lecture list')

    for c in Course.objects.filter(userset__slug=slug, lecture__semester=semester).distinct():
        # Create an array containing our courses and add the css class
        if c.id not in color_map:
            color_index = (color_index + 1) % MAX_COLORS
            color_map[c.id] = 'lecture%d' % color_index

        c.css_class = color_map[c.id]

        courses.append([c, group_forms.get(c.id)])

    t.tick('Done building course array')

    t.tick('Starting main lecture loop')
    for i,lecture in enumerate(initial_lectures.exclude(excluded_from__slug=slug)):
        # Our actual layout algorithm for handling collisions and displaying in
        # tables:

        start = lecture.start_time - Lecture.START[0][0]
        end = lecture.end_time - Lecture.END[0][0]
        rowspan = end - start + 1

        first = start

        # Keep track of which lectures are displayed
        included.append(lecture.id)

        # Try to find leftmost row that can fit our lecture, if we run out of
        # rows to test, ie IndexError, we append a fresh one to work with
        try:
            row = 0
            while start <= end:
                if table[start][lecture.day][row]:
                    # One of our time slots is taken, bump the row number and
                    # restart our search
                    row += 1
                    start = first
                else:
                    start += 1

        except IndexError:
            # We ran out of rows to check, simply append a new row
            for j,time in enumerate(Lecture.START):
                table[j][lecture.day].append({})

            # Update the header colspan
            span[lecture.day] += 1

        start = first
        remove = False

        css = [color_map[lecture.course_id]]

        if lecture.type.optional:
            css.append('optional')

        if rowspan == 1:
            css.append('single')

        while start <= end:
            # Replace the cell we found with a base containing info about our
            # lecture
            table[start][lecture.day][row] = {
                'lecture': lecture,
                'rowspan': rowspan,
                'remove': remove,
                'class': ' '.join(css),
            }

            # Add lecture to our supplementary data structure and set the
            # remove flag.
            if not remove:
                remove = True
                lectures.append({
                    'lecture': lecture,
                    'height': rowspan,
                    'i': start,
                    'j': lecture.day,
                    'm': row,
                })

            start += 1

    t.tick('Starting lecture expansion')
    for lecture in lectures:
        # Loop over supplementary data structure using this to figure out which
        # colspan expansions are safe
        i = lecture['i']
        j = lecture['j']
        m = lecture['m']
        height = lecture['height']

        expand_by = 1

        # Find safe expansion of colspan
        safe = True
        for l in xrange(m+1, len(table[i][j])):
            for k in xrange(i,i+height):
                if table[k][j][l]:
                    safe = False
                    break
            if safe:
                expand_by += 1
            else:
                break

        table[i][j][m]['colspan'] = expand_by

        # Remove cells that will get replaced by colspan
        for l in xrange(m+1,m+expand_by):
            for k in xrange(i,i+height):
                table[k][j][l]['remove'] = True
    t.tick('Done with lecture expansion')

    # FIXME add second round of expansion equalising colspan

    # Insert extra cell containg times
    for i,start,end in map(lambda x,y: (x[0], x[1][1],y[1]), enumerate(Lecture.START), Lecture.END):
        table[i].insert(0, [{'time': '%s - %s' % (start, end), 'class': 'time'}])
    t.tick('Done adding times')

    if advanced:
        # Add colors and exlude status
        for i,lecture in enumerate(initial_lectures):
            initial_lectures[i].css_class =  color_map[lecture.course_id]
            initial_lectures[i].excluded  =  lecture.id not in included

            initial_lectures[i].sql_weeks = weeks.get(lecture.id, [])
            initial_lectures[i].sql_groups = groups.get(lecture.id, [])
            initial_lectures[i].sql_lecturers = lecturers.get(lecture.id, [])

        t.tick('Done lecture css_clases and excluded status')

    t.tick('Starting render to response')
    response = render_to_response('common/schedule.html', {
                            'advanced': advanced,
                            'colspan': span,
                            'courses': courses,
                            'lectures': initial_lectures,
                            'legend': map(lambda x: x[0], courses),
                            'semester': semester,
                            'slug': slug,
                            'table': table,
                        }, RequestContext(request))

    t.tick('Saving to cache')
    cache.set(cache_key, response)

    t.tick('Returning repsonse')
    return response

def select_groups(request, year, type, slug):
    type = dict(map(lambda x: (x[1],x[0]), Semester.TYPES))[type.lower()]
    semester = Semester.objects.get(year=year, type=type)

    if request.method == 'POST':
        for c in Course.objects.filter(userset__slug=slug).distinct().order_by('id'):
            group_form = GroupForm(Group.objects.filter(lecture__course=c).distinct(), request.POST, prefix=c.id)

            if group_form.is_valid():
                set = UserSet.objects.get(course=c, slug=slug, semester=semester)
                set.groups = group_form.cleaned_data['groups']

        cache_key = ':'.join(['schedule', year, semester.get_type_display(), slug])
        cache.delete(cache_key+':False')
        cache.delete(cache_key+':True')

        logging.debug('Deleted cache key: %s' % cache_key)

    return HttpResponseRedirect(reverse('schedule-advanced', args=[semester.year,semester.get_type_display(),slug]))

def select_course(request, year, type, slug):
    type = dict(map(lambda x: (x[1],x[0]), Semester.TYPES))[type.lower()]
    semester = Semester.objects.get(year=year, type=type)

    if request.method == 'POST' and 'course' in request.POST:

        cache_key = ':'.join(['schedule', year, semester.get_type_display(), slug])
        cache.delete(cache_key+':False')
        cache.delete(cache_key+':True')

        logging.debug('Deleted cache key: %s' % cache_key)

        if 'submit_add' in request.POST:
            lookup = request.POST['course'].split()

            errors = []

            for l in lookup:
                try:
                    course = Course.objects.get(name__iexact=l.strip())

                    if not course.lecture_set.count():
                        scrape(request, l, no_auth=True)

                    userset, created = UserSet.objects.get_or_create(slug=slug, course=course, semester=semester)

                    for g in Group.objects.filter(lecture__course=course).distinct():
                        userset.groups.add(g)

                except:
                    errors.append(l)

            if errors:
                return render_to_response('common/error.html',
                            {'courses': errors, 'slug': slug, 'year': year, 'type': semester.get_type_display()},
                            RequestContext(request))

        elif 'submit_remove' in request.POST:
            courses = [c.strip() for c in request.POST.getlist('course') if c.strip()]
            sets = UserSet.objects.filter(slug__iexact=slug, course__id__in=courses)
            sets.delete()

    return HttpResponseRedirect(reverse('schedule-advanced', args=[semester.year, semester.get_type_display(), slug]))

def select_lectures(request, year,type,slug):
    if request.method == 'POST':
        excludes = request.POST.getlist('exclude')

        for userset in UserSet.objects.filter(slug=slug):
            userset.exclude = userset.course.lecture_set.filter(id__in=excludes)

        cache_key = ':'.join(['schedule', year, type, slug])
        cache.delete(cache_key+':False')
        cache.delete(cache_key+':True')

        logging.debug('Deleted cache key: %s' % cache_key)

    return HttpResponseRedirect(reverse('schedule-advanced', args=[year,type,slug]))

# FIXME take in semester object
def scrape_list(request):
    '''Scrape the NTNU website to retrive all available courses'''
    if not request.user.is_authenticated():
        raise Http404

    opener = URLopener()
    opener.addheader('Accept', '*/*')

    html = ''.join(opener.open('http://www.ntnu.no/studier/emner').readlines())
    soup = BeautifulSoup(html)

    courses = []
    course_ids = []
    for a in soup.findAll('div', id="browseForm")[0].findAll('a'):
        contents = a.contents[0].strip()

        if contents.endswith('(Nytt)'):
            contents = contents[:-len('(Nytt)')]

        m = re.match(r'^\s*(.+)\((.+)\)\s*$', contents)

        if m:
            courses.append(m.group(2,1))

    for name,full_name in courses:
        course, created = Course.objects.get_or_create(name=name.strip().upper())

        if not course.full_name:
            course.full_name = full_name.strip()
            course.save()

        course_ids.append(course.id)

    return HttpResponse(str('\n'.join([str(c) for c in courses])), mimetype='text/plain')

def scrape_exam(request, no_auth=False):
    # FIXME get into working shape
    if not request.user.is_authenticated() and not no_auth:
        raise Http404

    url = 'http://www.ntnu.no/eksamen/plan/08h/'

    html = ''.join(urlopen(url).readlines())
    soup = BeautifulSoup(html)

    main = soup.findAll('div', 'hovedramme')[0]

    results = []
    for tr in main.findAll('tr')[4:]:
        results.append({})

        for i,td in enumerate(tr.findAll('td')):
            if i == 0:
                results[-1]['course'] = td.contents
            elif i == 2:
                results[-1]['type'] = td.contents
            elif i == 3:
                results[-1]['time'] = td.findAll(text=lambda text: isinstance(text, NavigableString))
            elif i == 4:
                results[-1]['duration'] = td.contents
            elif i == 5:
                results[-1]['comment'] = td.contents

    for r in results:
        course, created = Course.objects.get_or_create(name=r['course'][0])

        try:

            if r['duration']:
                duration = r['duration'][0]
            else:
                duration = None

            if r['comment']:
                comment = r['comment'][0]
            else:
                comment = ''

            if ':' in r['time'][0]:
                date,time = r['time'][0].split()
                date = date.split('.')
                time = time.split(':')

                time = datetime(int(date[2]), int(date[1]), int(date[0]), int(time[0]), int(time[1]))
            else:
                time = r['time'][0].split('.')
                time = datetime(int(date[2]), int(date[1]), int(date[0]), int(time[0]))

            # FIXME handle multiple exams
            if r['type']:
                exam = Exam(course=course, type=r['type'].pop(), time=time, comment=comment, duration=duration)
                exam.save()
        except:
            pass

    return HttpResponse(str('\n'.join([str(r) for r in results])), mimetype='text/plain')

# FIXME take semester as parameter
def scrape(request, course, no_auth=False):
    '''Retrive all lectures for a given course'''
    if not no_auth and not request.user.is_authenticated():
        raise Http404

    # FIXME based on semester
    url  = 'http://www.ntnu.no/studieinformasjon/timeplan/h08/?emnekode=%s' % course.upper().strip()

    errors = []

    for number in [1,2,3]:
        html = ''.join(urlopen('%s-%d' % (url, number)).readlines())
        soup = BeautifulSoup(html)
        main = soup.findAll('div', 'hovedramme')[0]
        table = main.findAll('table')[1]

        results = []

        text_only = lambda text: isinstance(text, NavigableString)

        try:
            title = table.findAll('h2')[0].contents[0].split('-')[2].strip()

            errors = []
            break

        except IndexError:
            errors.append(('Course does not exsist', '%s-%d' % (url, number)))

    if errors:
        raise Exception(errors)

    type = None
    for tr in table.findAll('tr')[2:-1]:
        time, weeks, room, lecturer, groups  = [], [], [], [], []
        lecture = True

        for i,td in enumerate(tr.findAll('td')):
            # Loop over our td's basing our action on the td's index in the tr
            # element.
            if i == 0:
                if td.get('colspan') == '4':
                    type = td.findAll(text=text_only)
                    lecture = False
                    break
                else:
                    for t in td.findAll('b')[0].findAll(text=text_only):
                        day, period = t.split(' ', 1)
                        start, end = [x.strip() for x in period.split('-')]
                        time.append([day,start,end])

                    for week in td.findAll('span')[0].contents[0].split()[1:]:
                        if '-' in week:
                            x,y = week.split('-')
                            weeks.extend(range(int(x),int(y)))
                        else:
                            weeks.append(int(week.replace(',', '')))
            elif i == 1:
                [room.extend(a.findAll(text=text_only)) for a in td.findAll('a')]
            elif i == 2:
                lecturer = [l.replace('&nbsp;', '') for l in td.findAll(text=text_only)]
            elif i == 3:
                groups = [p for p in td.findAll(text=text_only) if p.replace('&nbsp;','').strip()]

        if lecture:
            results.append({
                'type': type,
                'time': time,
                'weeks': weeks,
                'room': room,
                'lecturer': lecturer,
                'groups': groups,
                'title': title,
            })

    semester = Semester.objects.all()[0]
    course, created = Course.objects.get_or_create(name=course.upper())

    for r in results:
        if not course.full_name:
            course.full_name = r['title']
            course.save()
        room, created = Room.objects.get_or_create(name=r['room'][0])
        type, created = Type.objects.get_or_create(name=r['type'][0])

        day = ['Mandag', 'Tirsdag', 'Onsdag', 'Torsdag', 'Fredag'].index(r['time'][0][0])

        # We choose to be slightly naive and only care about which hour
        # something starts.
        start = int(r['time'][0][1].split(':')[0])
        end = int(r['time'][0][2].split(':')[0])

        start = dict(map(lambda x: (int(x[1].split(':')[0]),x[0]), Lecture.START))[start]
        end = dict(map(lambda x: (int(x[1].split(':')[0]),x[0]), Lecture.END))[end]

        lecture, created = Lecture.objects.get_or_create(
            course=course,
            day=day,
            semester=semester,
            start_time=start,
            end_time=end,
            room = room,
            type = type,
        )
        r['id'] = lecture.id

        if r['groups']:
            for g in r['groups']:
                group, created = Group.objects.get_or_create(name=g)
                lecture.groups.add(group)
        else:
            group, created = Group.objects.get_or_create(name=Group.DEFAULT)
            lecture.groups.add(group)

        for w in  r['weeks']:
            week, created = Week.objects.get_or_create(number=w)
            lecture.weeks.add(w)

        for l in r['lecturer']:
            if l.strip():
                lecturer, created = Lecturer.objects.get_or_create(name=l)
                lecture.lecturers.add(lecturer)

        lecture.save()

    return HttpResponse(str('\n'.join([str(r) for r in results])), mimetype='text/plain')

def ical(request, year, semester, slug):
    semester = dict(map(lambda x: (x[1],x[0]), Semester.TYPES))[semester.lower()]
    semester = Semester.objects.get(year=year, type=semester)

    cal = vobject.iCalendar()
    cal.add('method').value = 'PUBLISH'  # IE/Outlook needs this

    for l in Lecture.objects.filter(course__userset__slug=slug):
        weeks = l.weeks.values_list('number', flat=True)

        for d in rrule(WEEKLY,byweekno=weeks,count=len(weeks),byweekday=l.day,dtstart=datetime(int(year),1,1)):
            vevent = cal.add('vevent')
            vevent.add('summary').value = l.course.name
            vevent.add('location').value = l.room.name
            vevent.add('categories').value = [l.type.name]

            (hour, minute) = l.get_start_time_display().split(':')
            vevent.add('dtstart').value = datetime(d.year, d.month, d.day, int(hour), int(minute))

            (hour, minute) = l.get_end_time_display().split(':')
            vevent.add('dtend').value = datetime(d.year, d.month, d.day, int(hour), int(minute))

            vevent.add('dtstamp').value = datetime.now()

    icalstream = cal.serialize()
    if 'plain' in request.GET:
        response = HttpResponse(icalstream, mimetype='text/plain')
    else:
        response = HttpResponse(icalstream, mimetype='text/calendar')
        response['Filename'] = '%s.ics' % slug  # IE needs this
        response['Content-Disposition'] = 'attachment; filename=filename.ics'

    return response

