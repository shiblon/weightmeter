from __future__ import division

import csv
import datetime
import logging
import math
import os.path
import re
import wsgiref.handlers

from StringIO import StringIO

from datamodel import UserInfo, WeightBlock, WeightData, DEFAULT_QUERY_DAYS
from datamodel import sample_entries
from datamodel import decaying_average_iter, full_entry_iter
from graph import chartserver_bounded_size, chartserver_weight_url
from wsgiutil import PathRequestHandler, ParamSanitizer, escape_qp
from urlparse import urlparse, urlunparse

from google.appengine.api import users
from google.appengine.ext import webapp, db
from google.appengine.ext.webapp import template

# Set constants
DEFAULT_SELECT_DAYS = 14
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

class Error(PathRequestHandler):
  path_regex = '/error'

  def GET(self):
    self.out.write("An error occurred while trying to go to an error page.")

class UpdateEntry(PathRequestHandler):
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

class MobileSite(PathRequestHandler):
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

    path = os.path.join(os.path.dirname(__file__), 'index.html')
    self.response.out.write(template.render(path, template_values))

class DebugOutput(PathRequestHandler):
  path_regex = '/debug'
  def GET(self):
    user_info = _get_current_user_info()
    query = WeightBlock.all()
    entries = []
    day_delta = datetime.timedelta(days=1)
    for block in query:
      start_date = datetime.date.fromordinal(block.day_zero)
      for i, weight in enumerate(block.weight_entries):
        if weight >= 0.0:
          entries.append((start_date + day_delta * i, weight))

    # Output to the template
    template_values = {
        'user_info': user_info,
        'entries': entries,
        }

    path = os.path.join(os.path.dirname(__file__), 'debug_index.html')
    self.response.out.write(template.render(path, template_values))

class DataImport(PathRequestHandler):
  path_regex = '/data'

  def POST(self):
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

  def GET(self):
    template_values = {
        'user_name': users.get_current_user().email(),
        'logout_url': users.create_logout_url(self.request.uri),
        'settings_url': '/settings',
        'index_url': '/index',
        'csv_link': '/csv?s=*',
        }

    path = os.path.join(os.path.dirname(__file__), 'data.html')
    self.response.out.write(template.render(path, template_values))

class Settings(PathRequestHandler):
  path_regex = '/settings'
  default_error_path = '/settings'
  default_redir_path = '/settings'

  def POST(self):
    user_info = _get_current_user_info()

    form = ParamSanitizer(self.request,
                          ('resolution', ParamSanitizer.Float),
                          ('gamma', ParamSanitizer.Float),
                          ('r', ParamSanitizer.URIPath, ''),
                          ('e', ParamSanitizer.URIPath, ''),
                          default_on_error=True)

    if form.failure():
      logging.debug("Sanitizer failed on %r", form.failed)
      failed_url = first_nonempty(form.params['e'],
                                  form.params['r'],
                                  self.default_error_path,
                                  DEFAULT_ERROR_PATH)
      self.safe_redirect(form.failed_redirect_url(failed_url))
    else:
      user_info.scale_resolution = form.params['resolution']
      user_info.gamma = form.params['gamma']
      user_info.put()
      redir_url = first_nonempty(form.params['r'],
                                 self.default_redir_path,
                                 DEFAULT_REDIR_PATH,
                                 overrides={'state': 'success'})
      self.safe_redirect(redir_url)

  def GET(self):
    user_info = _get_current_user_info()

    float_format = "%0.2f"

    # Decay weight options
    gamma_options = []
    val = 0.5
    while val < 1.0:
      gamma_options.append({'value': float_format % val})
      val += 0.05

    for opt in gamma_options:
      logging.info("Option: %s", opt)
      if opt['value'] == (float_format % user_info.gamma):
        opt['selected'] = True

    # Scale resolution options
    resolution_options = [
        0.1,
        0.2,
        0.25,
        0.5,
        1.0,
        ]

    for i, value in enumerate(resolution_options):
      opt = resolution_options[i] = {'value': float_format % value}
      if opt['value'] == (float_format % user_info.scale_resolution):
        opt['selected'] = True

    template_values = {
        'user_name': users.get_current_user().email(),
        'index_url': '/index',
        'data_url': '/data',
        'gamma_options': gamma_options,
        'resolution_options': resolution_options,
        'state': self.request.get('state', None),
        }

    path = os.path.join(os.path.dirname(__file__), 'settings.html')
    self.response.out.write(template.render(path, template_values))

class CsvDownload(PathRequestHandler):
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

def main():
  template.register_template_library('templatestuff')
  application = webapp.WSGIApplication(
      [MobileSite.wsgi_handler(),
       UpdateEntry.wsgi_handler(),
       Settings.wsgi_handler(),
       DebugOutput.wsgi_handler(),
       DataImport.wsgi_handler(),
       CsvDownload.wsgi_handler(),
       Error.wsgi_handler(),
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
