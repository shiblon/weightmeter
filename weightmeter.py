from __future__ import division

import csv
import datetime
import logging
import math
import os.path
import re
import wsgiref.handlers

from StringIO import StringIO

from google.appengine.api import users
from google.appengine.ext import webapp, db
from google.appengine.ext.webapp import template

# This has to come after the appengine includes, otherwise the appropriate
# environment is not yet set up for Django and it barfs.
try:
  from django import newforms as forms
except ImportError:
  from django import forms

from datamodel import UserInfo, WeightBlock, WeightData, DEFAULT_QUERY_DAYS
from datamodel import sample_entries
from datamodel import decaying_average_iter, full_entry_iter
from dateutil import DateDelta, dates_from_path
from graph import chartserver_bounded_size, chartserver_weight_url
from urlparse import urlparse, urlunparse
from util.forms import FloatChoiceField, FloatField, DateSelect
from util.handlers import RequestHandler
from wsgiutil import ParamSanitizer, escape_qp

def template_path(name):
  return os.path.join(os.path.dirname(__file__), 'templates', name)

# Set constants
DEFAULT_SELECT_DAYS = 14
DEFAULT_GRAPH_DURATION = '2w'
DEFAULT_POUND_SELECTION = 2

DEFAULT_ERROR_PATH = '/error'
DEFAULT_REDIR_PATH = '/index'

MOBILE_IMG_WIDTH = 300
MOBILE_IMG_HEIGHT = 200

MAX_GRAPH_SAMPLES = 200

def first_nonempty(*args):
  for a in args:
    if a:
      return a
  else:
    return None

def _get_current_user_info():
  user = users.get_current_user()
  assert user is not None
  return UserInfo.get_or_insert('u:' + user.email(), user=user)

def _date_range_from_params(start_param, end_param, today=None):
  """Return today, start_date, and end_date for the URL parameters"""

  if today is None:
    today = get_today()

  today = today.toordinal()

  def day_from_param(param):
    if param == '*':
      return None

    day = None
    try:
      day = int(param)
      if day <= 0:
        day += today
    except ValueError, e:
      day = datetime.datetime.strptime("%Y-%m-%d").date().toordinal()

    return day

  start_day = day_from_param(start_param)
  end_day = day_from_param(end_param)

  if start_day is None:
    start_day = datetime.date(1900, 1, 1).toordinal()

  if end_day is None:
    end_day = today.toordinal()

  return [datetime.date.fromordinal(x) for x in (today, start_day, end_day)]

class WeightEntryForm(forms.Form):
  date = forms.ChoiceField(widget=DateSelect)
  weight = FloatField(max_length=7, widget=forms.TextInput(attrs={'size': 5}))

class ShowGraph(RequestHandler):
  def get(self, mpath, spath='', epath=''):
    """Get the graph page.

    Params:
      mpath - will be /m if this should be a mobile page (for handhelds)
      spath - start date path component (optional)
      epath - end date path component (optional)
    """
    # TODO: Time zone
    today = datetime.date.today()
    # TODO: make a user_info setting for the default duration
    if spath.strip('/').lower() == 'all':
      sdate = datetime.date(1990, 1, 1)  # basically everything
      edate = today
    else:
      sdate, edate = dates_from_path(spath, epath, today,
                                     default_start=DEFAULT_GRAPH_DURATION)
    is_mobile = bool(mpath)

    # Get the settings and info for this user
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)

    # TODO: rethink this whole param sanitizer thing
    sanitizer = ParamSanitizer(
      self.request,
      ('w', ParamSanitizer.Integer, MOBILE_IMG_WIDTH),
      ('h', ParamSanitizer.Integer, MOBILE_IMG_HEIGHT),
      ('es', ParamSanitizer.Enumeration(('l', 't')), 'l'),
      default_on_error=True)

    img_width = sanitizer.params['w']
    img_height = sanitizer.params['h']
    chart_width, chart_height = chartserver_bounded_size(img_width, img_height)
    samples = min(MAX_GRAPH_SAMPLES, chart_width // 4)

    smoothed_iter = weight_data.smoothed_weight_iter(sdate,
                                                     edate,
                                                     samples,
                                                     gamma=user_info.gamma)
    # Make a chart
    img = {
        'width': img_width,
        'height': img_height,
        'url': chartserver_weight_url(chart_width, chart_height, smoothed_iter),
        }
    logging.debug("Graph Chart URL: %s", img['url'])

    recent_entry = weight_data.most_recent_entry()
    recent_weight = None
    if recent_entry:
      recent_weight = recent_entry[1]
    form = WeightEntryForm(initial={'weight': recent_weight, 'date': today})

    # Output to the template
    template_values = {
        'img': img,
        'user': users.get_current_user(),
        'form': form,
        'durations': ('All', '1y', '6m', '3m', '2m', '1m', '2w', '1w'),
        }

    path = template_path('index.html')
    self.response.out.write(template.render(path, template_values))

class Error(RequestHandler):
  path_regex = '/error'

  def GET(self):
    self.out.write("An error occurred while trying to go to an error page.")

class UpdateEntry(RequestHandler):
  path_regex = '/update'
  default_error_path = '/update'
  default_redir_path = '/update'

  def POST(self):
    """Updates a single weight entry."""
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)

    form = ParamSanitizer(self.request,
                          ('date', ParamSanitizer.Date),
                          ('weight', ParamSanitizer.Number),
                          ('r', ParamSanitizer.URIPath, ''),
                          ('e', ParamSanitizer.URIPath, ''),
                          default_on_error=True)

    if form.failure():
      logging.debug("Sanitizer failed on %r", form.failed)
      failure_url = first_nonempty(form.params['e'],
                                   form.params['r'],
                                   self.default_error_path,
                                   DEFAULT_ERROR_PATH)
      self.safe_redirect(form.failed_redirect_url(failure_url))
    else:
      date = form.params['date']
      weight = form.params['weight']
      weight_data.update(date, weight)
      redir_url = first_nonempty(form.params['r'],
                                 self.default_redir_path,
                                 DEFAULT_REDIR_PATH)
      self.safe_redirect(redir_url)

class MobileSite(RequestHandler):
  path_regex = '^/m(/.*|)'

  def GET(self, subpath):
    self.safe_redirect(self.add_to_request_path('/index'), True)

  def GET_index(self, subpath):
    logging.info("%s", self.request.path)

    # Get the settings and info for this user
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)

    sanitizer = ParamSanitizer(
      self.request,
      ('s', sanitize_date_range_ordinal, -DEFAULT_SELECT_DAYS),
      ('e', ParamSanitizer.Integer, 0),
      ('w', ParamSanitizer.Integer, MOBILE_IMG_WIDTH),
      ('h', ParamSanitizer.Integer, MOBILE_IMG_HEIGHT),
      ('es', ParamSanitizer.Enumeration(('l', 't')), 'l'),
      default_on_error=True)

    # Keep the error stuff separate since we may want to treat it differently
    # (not propagate it to links on the page, etc.).  Default to None, an
    # invalid error param string, so that success means that there is an error
    # message parameter to do stuff with.
    error_sanitizer = ParamSanitizer(self.request,
                                     ('E', ParamSanitizer.ErrorParams, None))

    img_width = sanitizer.params['w']
    img_height = sanitizer.params['h']
    chart_width, chart_height = chartserver_bounded_size(img_width, img_height)
    logging.debug("cw=%d, ch=%d", chart_width, chart_height)
    samples = min(MAX_GRAPH_SAMPLES, chart_width // 4)

    # Note that the failure of the sanitizer is not too critical in this case:
    # we will just silently fall back on the defaults.
    # In fact, because defaults are specified for every entry above, and we
    # allow defaults when sanitization fails, an error can't occur here.

    # Get date and view ranges
    today, start, end = _date_range_from_params(sanitizer.params['s'],
                                                sanitizer.params['e'])

    smoothed_iter = weight_data.smoothed_weight_iter(start,
                                                     end,
                                                     samples,
                                                     gamma=user_info.gamma)

    # Make a chart
    img = {
        'width': img_width,
        'height': img_height,
        'url': chartserver_weight_url(chart_width, chart_height, smoothed_iter),
        }

    logging.debug("Chart URL: %s", img['url'])

    # Get the most recent weight entry and create a selection list
    recent_entry = weight_data.most_recent_entry()
    weight_format = "%.2f"
    weight_choices = None
    if recent_entry is not None:
      logging.debug("recent entry: %r", recent_entry)
      # Make a selection box that centers on this, with DEFAULT_POUND_SELECTION
      # pounds either direction to choose from
      recent_date, recent_weight = recent_entry
      start_weight = math.floor(recent_weight - DEFAULT_POUND_SELECTION)
      end_weight = math.ceil(recent_weight + DEFAULT_POUND_SELECTION)
      weight = start_weight
      weight_choices = []
      while weight <= end_weight:
        choice = weight_format % weight
        weight_choices.append(choice)
        weight += user_info.scale_resolution

    # Set up the date selection list
    day_delta = datetime.timedelta(days=1)
    dates = [today - i * day_delta for i in xrange(-1, DEFAULT_SELECT_DAYS)]
    date_items = [
        {'date': d.strftime("%Y-%m-%d"),
         'name': d.strftime("%a, %b %d"),
         'selected': d == today} for d in dates]

    start_param = sanitizer.params['s']
    entry_style = sanitizer.params['es']

    view_options = [
        {'name': 'All', 'start': '*'},
        {'name': '1y', 'start': '-365'},
        {'name': '6m', 'start': '-180'},
        {'name': '3m', 'start': '-90'},
        {'name': '2m', 'start': '-60'},
        {'name': '1m', 'start': '-30'},
        {'name': '2w', 'start': '-14'},
        {'name': '1w', 'start': '-7'},
        ]

    # Add junk to the view options (calculate URL, etc.)
    for opt in view_options:
      if opt['start'] == start_param:
        opt['selected'] = True
      opt['url'] = sanitizer.redirect_string(self.request.path,
                                             {'s': opt['start'],
                                              'e': '0'})

    entry_styles = [
        {'name': 'text', 'param': 't'},
        {'name': 'list', 'param': 'l'}
        ]

    for style in entry_styles:
      if style['param'] == entry_style:
        style['selected'] = True
      style['url'] = sanitizer.redirect_string(self.request.path,
                                               {'s': start_param,
                                                'es': style['param']})

    recent_weight = None
    if recent_entry:
      recent_weight = weight_format % recent_entry[1]

    # Create a URL back here:
    self_redirect = sanitizer.redirect_string(self.request.path)

    # Output to the template
    template_values = {
        'has_error': error_sanitizer.success(),
        'error_params': error_sanitizer.params.get('E', None),
        'img': img,
        'settings_url': '/settings',
        'logout_url': users.create_logout_url(self.request.uri),
        'data_url': '/data',
        'user_name': users.get_current_user().email(),
        'update_url': '/update?r=%s' % escape_qp(self_redirect),
        'weight_choices': weight_choices,
        'recent_weight': recent_weight,
        'date_items': date_items,
        'text_entry': sanitizer.params['es'] == 't' or not recent_entry,
        'view_options': view_options,
        'entry_styles': entry_styles,
        }

    path = template_path('index.html')
    self.response.out.write(template.render(path, template_values))

class DataImport(RequestHandler):
  def POST(self, *args):
    # TODO: use param sanitizer
    # TODO: Create a CSV data sanitizer
    # TODO: verify that the file format is correct
    posted_data = ''
    
    if self.request.get('entries_csv_file'):
      posted_data = self.request.POST['entries_csv_file'].value
    if posted_data and not posted_data.endswith('\n'):
      posted_data += '\n'
    posted_data += self.request.get('entries_csv_text', '')

    if not posted_data:
      # TODO: show an error message
      self.redirect('/data')
      return

    # Try all different CSV formats, starting with comma-delimited.  Break if
    # one of them works on the first row, then assume that all other rows will
    # use that delimiter.
    data_file = StringIO(posted_data)
    for delimiter in ', \t':
      logging.debug("CSV import: trying delimiter '%s'", delimiter)
      data_file.seek(0)
      reader = csv.reader(data_file, delimiter=delimiter)
      try:
        logging.debug("CSV import: trying a row")
        row = []
        while len(row) == 0:
          row = reader.next()
          if len(row) == 1:  # delimiter failed, or invalid format
            raise csv.Error("Invalid data for delimiter '%s'" % delimiter)
        # Found one that works, so start over, create the new reader, and bail
        data_file.seek(0)
        reader = csv.reader(data_file, delimiter=delimiter)
        break
      except csv.Error, e:
        logging.warn("CSV delimiter '%s' invalid for uploaded data", delimiter)
    else:
      data_file.seek(0)
      logging.error("Unrecognized csv format: '%s'", data_file.readline())
      # TODO: show an error to the user in some meaningful way
      self.redirect('/data')

    entries = []
    for row in reader:
      if len(row) < 2:
        logging.error("Invalid row in imported data '%s'", ",".join(row))
        continue
      else:
        datestr, weightstr = row[:2]

        if weightstr in ('-', '_', ''):
          weightstr = '-1'  # replace with invalid float

        if '/' in datestr:
          format = "%m/%d/%Y"
        else:
          format = "%Y-%m-%d"

        try:
          date = datetime.datetime.strptime(datestr, format).date()
        except ValueError, e:
          logging.error("Invalid date entry '%s'", datestr)
          continue

        try:
          weight = float(weightstr)
        except ValueError, e:
          logging.error("Invalid weight entry '%s'", weightstr)
          continue

      entries.append((date, weight))

    # Now we shove the entries into the database
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)
    weight_data.batch_update(entries)

    self.redirect(self.request.get('redir_url', '/index'))

  def get(self, mpath):
    template_values = {
        'user': users.get_current_user(),
        'logout_url': users.create_logout_url(self.request.uri),
        'csv_link': '/csv?s=*',
        }

    path = template_path('data.html')
    self.response.out.write(template.render(path, template_values))

class SettingsForm(forms.Form):
  scale_resolution = FloatChoiceField(
    initial=.5,
    floatfmt='%0.02f',
    float_choices=[.1, .2, .25, .5, 1.],
  )
  gamma = FloatChoiceField(
    initial=.9,
    floatfmt='%0.02f',
    float_choices=(.7, .75, .8, .85, .9, .95, 1.),
    label="Decay weight",
  )

class Settings(webapp.RequestHandler):
  def _render(self, mpath, user_info, form):
    template_values = {
      'user': users.get_current_user(),
      'index_url': '/index',
      'data_url': '/data',
      'form': form,
      'is_mobile': bool(mpath),
    }

    path = template_path('settings.html')
    self.response.out.write(template.render(path, template_values))

  def post(self, mpath):
    user_info = _get_current_user_info()
    form = SettingsForm(self.request.POST)
    if not form.is_valid():
      # Errors?  Just render the form: the POST didn't do anything, so a
      # refresh would be expected to "retry"
      return self._render(mpath, user_info, form)
    else:
      # No errors, store the data
      user_info.scale_resolution = form.clean_data['scale_resolution']
      user_info.gamma = form.clean_data['gamma']
      user_info.put()

      # Send the user to the default front page after settings are altered.
      return self.redirect(ShowGraph.get_url(mpath))

  def get(self, mpath):
    user_info = _get_current_user_info()
    form = SettingsForm(initial=user_info.__dict__)
    return self._render(mpath, user_info, form)

class CsvDownload(RequestHandler):
  path_regex = '/csv'

  def GET(self):
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)

    # Get today's date and view ranges (default to everything)
    today, start_date, end_date = _date_range_from_params(
        self.request.get('s', '*'),
        self.request.get('e', '0'))

    self.response.headers['Content-Type'] = 'application/octet-stream'
    self.response.headers.add_header('Content-Disposition', 'attachment', filename='weight.csv')

    writer = csv.writer(self.response.out)
    writer.writerows(list(weight_data.query(start_date, end_date)))

def get_today():
  # TODO: Make this grok user time zone
  return datetime.date.today()

def sanitize_date_range_ordinal(val):
  return val if val == '*' else ParamSanitizer.Integer(val)

class Logout(RequestHandler):
  def get(self, mpath):
    self.redirect(users.create_logout_url(mpath))

class DefaultRoot(RequestHandler):
  def get(self, mpath):
    self.redirect(ShowGraph.get_url(mpath))

def main():
  template.register_template_library('templatestuff')
  # In order to allow for a bare "graph" url, we specify it twice.  Simpler
  # that way.  Django gets less confused when path elements are truly optional.
  application = webapp.WSGIApplication(
      [
        ('(/m|)/?', DefaultRoot),
        ('(/m|)/graph', ShowGraph),
        ('(/m|)/graph/([^/]+)', ShowGraph),
        ('(/m|)/graph/([^/]+)/([^/]+)', ShowGraph),
        ('(/m|)/settings', Settings),
        ('(/m|)/logout', Logout),
        ('(/m|)/data', DataImport),
        MobileSite.wsgi_handler(),
        UpdateEntry.wsgi_handler(),
        CsvDownload.wsgi_handler(),
        Error.wsgi_handler(),
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
