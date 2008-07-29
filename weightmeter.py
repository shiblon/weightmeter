from __future__ import division

import cgi
import csv
import datetime
import logging
import math
import os.path
import wsgiref.handlers

from StringIO import StringIO

from datamodel import UserInfo, WeightBlock, WeightData, DEFAULT_QUERY_DAYS
from graph import chartserver_data_params
from graph import sample_entries
from graph import decaying_average_iter

from google.appengine.api import users
from google.appengine.ext import webapp, db
from google.appengine.ext.webapp import template

# Set constants
DEFAULT_SELECT_DAYS = 14
DEFAULT_POUND_SELECTION = 5

MOBILE_IMG_WIDTH = 300
MOBILE_IMG_HEIGHT = 200

MAX_GRAPH_SAMPLES = 100
MAX_MOBILE_SAMPLES = MOBILE_IMG_WIDTH // 4

def _get_current_user_info():
  user = users.get_current_user()
  assert user is not None
  return UserInfo.get_or_insert('u:' + user.email(), user=user)

def _date_range_from_params(start_param, end_param, today=None):
  """Return today, start_date, and end_date for the URL parameters"""

  if today is None:
    today = datetime.date.today()

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

class MainSite(webapp.RequestHandler):
  def get(self):
    template_values = {}

    path = os.path.join(os.path.dirname(__file__), 'index.html')
    self.response.out.write(template.render(path, template_values))

class DebugOutput(webapp.RequestHandler):
  def get(self):
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
    logging.info(entries)
    template_values = {
        'user_info': user_info,
        'entries': entries,
        }

    path = os.path.join(os.path.dirname(__file__), 'debug_index.html')
    self.response.out.write(template.render(path, template_values))

class DataImport(webapp.RequestHandler):
  def post(self):
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
      logging.info("Trying delimiter '%s'" % delimiter)
      data_file.seek(0)
      reader = csv.reader(data_file, delimiter=delimiter)
      try:
        logging.info("trying a row")
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
        logging.warn("CSV delimiter '%s' invalid for uploaded data" % delimiter)
    else:
      data_file.seek(0)
      logging.error("Unrecognized csv format: '%s'" % data_file.readline())
      # TODO: show an error to the user in some meaningful way
      self.redirect('/data')

    entries = []
    for row in reader:
      if len(row) < 2:
        logging.error("Invalid row in imported data '%s'" % ",".join(row))
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
          logging.error("Invalid date entry '%s'" % datestr)
          continue

        try:
          weight = float(weightstr)
        except ValueError, e:
          logging.error("Invalid weight entry '%s'" % weightstr)
          continue

      entries.append((date, weight))

    # Now we shove the entries into the database
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)
    weight_data.batch_update(entries)

    self.redirect(self.request.get('redir_url', '/index'))

  def get(self):
    template_values = {
        'user_name': users.get_current_user().email(),
        'logout_url': users.create_logout_url(self.request.uri),
        'settings_url': '/settings',
        'index_url': '/index',
        'csv_link': '/csv?s=*',
        }

    path = os.path.join(os.path.dirname(__file__), 'data.html')
    self.response.out.write(template.render(path, template_values))

class Settings(webapp.RequestHandler):
  def post(self):
    user_info = _get_current_user_info()

    user_info.scale_resolution = float(self.request.get('resolution'))
    user_info.gamma = float(self.request.get('gamma'))

    user_info.put()

    logging.info("Ready to redirect back to settings page")
    self.redirect('/settings?state=complete')

  def get(self):
    user_info = _get_current_user_info()

    float_format = "%0.02f"

    # Decay weight options
    gamma_options = []
    val = 0.5
    while val < 1.0:
      gamma_options.append({'value': float_format % val})
      val += 0.05

    for opt in gamma_options:
      logging.info(opt)
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

class Site(webapp.RequestHandler):
  def _today(self):
    # TODO: Make this grok user time zone
    return datetime.date.today()

  def _chart_params(self, smoothed_iter, width, height):
    params = [
        "chs=%dx%d" % (width, height),
        "cht=lc",
        "chm=D,ccddff,1,0,6|D,4488ff,0,0,2|d,4488ff,0,-1,6",
        ]
    params.extend(
        chartserver_data_params(
          smoothed_iter,
          width=width,
          height=height)
        )
    return params

  def get(self):
    logging.info(self.request.path)

    # The alias / just redirects to the viewing page: /index
    if self.request.path == '/':
      self.redirect('/index')
      return

    # Get the settings and info for this user
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)

    # Get date and view ranges

    today, start_date, end_date = _date_range_from_params(
        self.request.get('s', '%d' % (-DEFAULT_SELECT_DAYS,)),
        self.request.get('e', '0'),
        self._today())

    # Get the sampled raw weights and smoothed function:
    smoothed_iter = decaying_average_iter(
        sample_entries(
          weight_data.query(start_date, end_date),
          start_date,
          end_date,
          MAX_MOBILE_SAMPLES),
        gamma=user_info.gamma
        )

    # Make a chart
    img_width = MOBILE_IMG_WIDTH
    img_height = MOBILE_IMG_HEIGHT
    img_params = self._chart_params(smoothed_iter, img_width, img_height)
    img = {
        'width': img_width,
        'height': img_height,
        'url': "http://chart.apis.google.com/chart?" + "&".join(img_params)
        }

    logging.debug(img['url'])

    # Get the most recent weight entry and create a selection list
    recent_entry = weight_data.most_recent_entry()
    weight_format = "%.2f"
    weight_choices = None
    if recent_entry is not None:
      logging.debug("recent entry: %r" % (recent_entry,))
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
         'name': d.strftime("%B %d, %Y"),
         'selected': d == today} for d in dates]

    start_param = self.request.get('s', str(-DEFAULT_QUERY_DAYS))
    entry_style = self.request.get('es', 'l')

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
      opt['url'] = '/index?s=%s&es=%s' % (opt['start'], entry_style)

    entry_styles = [
        {'name': 'text', 'param': 't'},
        {'name': 'list', 'param': 'l'}
        ]

    for style in entry_styles:
      if style['param'] == entry_style:
        style['selected'] = True
      style['url'] = '/index?s=%s&es=%s' % (start_param, style['param'])

    recent_weight = None
    if recent_entry:
      recent_weight = weight_format % recent_entry[1]

    # Output to the template
    template_values = {
        'img': img,
        'settings_url': '/settings',
        'logout_url': users.create_logout_url(self.request.uri),
        'data_url': '/data',
        'user_name': users.get_current_user().email(),
        'weight_choices': weight_choices,
        'recent_weight': recent_weight,
        'date_items': date_items,
        'text_entry': self.request.get('es', 's') == 't' or not recent_entry,
        'view_options': view_options,
        'entry_styles': entry_styles,
        }

    path = os.path.join(os.path.dirname(__file__), 'index.html')
    self.response.out.write(template.render(path, template_values))

class AddEntry(webapp.RequestHandler):
  def post(self):
    user_info = _get_current_user_info()
    weight_data = WeightData(user_info)
    datestr = self.request.get('date')

    error = False
    try:
      date = datetime.datetime.strptime(datestr, "%Y-%m-%d").date()
    except ValueError, e:
      logging.error('Invalid date specified: %r' % datestr)
      error = True

    try:
      weight = float(self.request.get('weight'))
    except ValueError, e:
      logging.error("Invalid weight specified: %r" % self.request.get('weight'))
      error = True

    if not error:
      weight_data.update(date, weight)

    self.redirect('/index');

class CsvDownload(webapp.RequestHandler):
  def get(self):
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

def main():

  application = webapp.WSGIApplication(
      [('/', Site),
       ('/index', Site),
       ('/add_entry', AddEntry),
       ('/settings', Settings),
       ('/debug', DebugOutput),
       ('/data', DataImport),
       ('/csv', CsvDownload),
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
