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
from util.forms import FloatChoiceField, FloatField, DateSelectField
from util.handlers import RequestHandler
from wsgiutil import ParamSanitizer, escape_qp

# Set constants
DEFAULT_SELECT_DAYS = 14
DEFAULT_GRAPH_DURATION = '2w'
DEFAULT_POUND_SELECTION = 2

DEFAULT_ERROR_PATH = '/error'
DEFAULT_REDIR_PATH = '/index'

MOBILE_IMG_WIDTH = 300
MOBILE_IMG_HEIGHT = 200

MAX_GRAPH_SAMPLES = 200

def template_path(name):
  return os.path.join(os.path.dirname(__file__), 'templates', name)

def get_current_user_info():
  user = users.get_current_user()
  assert user is not None
  return UserInfo.get_or_insert('u:' + user.email(), user=user)

class WeightEntryForm(forms.Form):
  date = DateSelectField()
  weight = FloatField(max_length=7, widget=forms.TextInput(attrs={'size': 5}))

class ShowGraph(RequestHandler):
  def _render(self, mpath, spath, epath, user_info, form=None):
    # TODO: Time zone
    today = datetime.date.today()
    # TODO: make a user_info setting for the default duration
    sdate, edate = dates_from_path(spath, epath, today,
                                   default_start=DEFAULT_GRAPH_DURATION)
    is_mobile = bool(mpath)

    weight_data = WeightData(user_info)

    # TODO: rethink this whole param sanitizer thing
    sanitizer = ParamSanitizer(
      self.request,
      ('w', ParamSanitizer.Integer, MOBILE_IMG_WIDTH),
      ('h', ParamSanitizer.Integer, MOBILE_IMG_HEIGHT),
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
    if form is None:
      form = WeightEntryForm(initial={'weight': recent_weight, 'date': today})

    # Output to the template
    template_values = {
        'img': img,
        'user': users.get_current_user(),
        'form': form,
        'durations': ('All', '1y', '6m', '3m', '2m', '1m', '2w', '1w'),
        }

    path = template_path('index.html')
    return self.response.out.write(template.render(path, template_values))

  def get(self, mpath, spath='', epath=''):
    """Get the graph page.

    Params:
      mpath - will be /m if this should be a mobile page (for handhelds)
      spath - start date path component (optional)
      epath - end date path component (optional)
    """

    # Get the settings and info for this user
    user_info = get_current_user_info()
    self._render(mpath, spath, epath, user_info)

  def post(self, mpath, spath='', epath=''):
    """Updates a single weight entry."""
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)

    form = WeightEntryForm(self.request.POST)
    logging.debug("POST data: %r", self.request.POST)
    if not form.is_valid():
      logging.debug("Invalid form")
      return self._render(mpath, spath, epath, user_info, form)
    else:
      logging.debug("valid form")
      date = form.clean_data['date']
      weight = form.clean_data['weight']
      weight_data.update(date, weight)
      return self.redirect(ShowGraph.get_url(implicit_args=True))

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
    user_info = get_current_user_info()
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
    user_info = get_current_user_info()
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
      return self.redirect(ShowGraph.get_url(implicit_args=True))

  def get(self, mpath):
    user_info = get_current_user_info()
    form = SettingsForm(initial=user_info.__dict__)
    return self._render(mpath, user_info, form)

class CsvDownload(RequestHandler):
  def GET(self, spath='', epath=''):
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)

    today = datetime.date.today()
    sdate, edate = dates_from_path(spath, epath, today, default_start='all')

    self.response.headers['Content-Type'] = 'application/octet-stream'
    self.response.headers.add_header('Content-Disposition',
                                     'attachment',
                                     filename='weight.csv')

    # TODO: fix the weight data object to accept NULL for both dates to return
    # everything.  This may require doing multiple backend queries, but that
    # should be fine.
    writer = csv.writer(self.response.out)
    writer.writerows(list(weight_data.query(sdate, edate)))

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
        ('(/m|)/graph/([^/]+)/([^/]+)', ShowGraph),
        ('(/m|)/graph/([^/]+)', ShowGraph),
        ('(/m|)/graph', ShowGraph),
        ('(/m|)/settings', Settings),
        ('(/m|)/logout', Logout),
        ('(/m|)/data', DataImport),
        ('/csv/([^/]+)/([^/]+)', CsvDownload),
        ('/csv/([^/]+)', CsvDownload),
        ('/csv', CsvDownload),
        ('(/m|)/?', DefaultRoot),
        # TODO: add a default handler - 404
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
