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
from datamodel import sample_entries, decaying_average_iter, full_entry_iter
from graph import chartserver_bounded_size, chartserver_weight_url
from urlparse import urlparse, urlunparse
from util.dates import DateDelta, dates_from_args
from util.forms import FloatChoiceField
from util.forms import FloatField
from util.forms import DateSelectField
from util.forms import CSVWeightField
from util.handlers import RequestHandler
# TODO: get rid of this - make param sanitizer its own thing in the util
# directory
from wsgiutil import ParamSanitizer

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

class MobileGraph(RequestHandler):
  def _render(self, user_info, form=None):
    # TODO: Time zone
    today = datetime.date.today()

    start = self.request.get('s', '')
    end = self.request.get('e', '')

    # TODO: make a user_info setting for the default duration
    sdate, edate = dates_from_args(start, end, today,
                                   default_start=DEFAULT_GRAPH_DURATION)
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

    path = template_path('mobile_index.html')
    return self.response.out.write(template.render(path, template_values))

  def get(self):
    # Get the settings and info for this user
    user_info = get_current_user_info()
    self._render(user_info)

  def post(self):
    """Updates a single weight entry."""
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)

    form = WeightEntryForm(self.request.POST)
    logging.debug("POST data: %r", self.request.POST)
    if not form.is_valid():
      logging.debug("Invalid form")
      return self._render(user_info, form)
    else:
      logging.debug("valid form")
      date = form.clean_data['date']
      weight = form.clean_data['weight']
      weight_data.update(date, weight)
      return self.redirect(MobileGraph.get_url(implicit_args=True))

class Graph(RequestHandler):
  def get(self):
    return self.response.out.write("Main page")

class CSVFileForm(forms.Form):
  csvdata = CSVWeightField(widget=forms.FileInput)

class CSVTextForm(forms.Form):
  csvdata = CSVWeightField(widget=forms.Textarea(attrs={
                                                 'rows': 8,
                                                 'cols': 30,
                                                 }))

class MobileData(RequestHandler):
  def get(self):
    # TODO: Time zone
    today = datetime.date.today()
    # TODO: make a user_info setting for the default duration
    start = self.request.get('s', '')
    end = self.request.get('e', '')
    sdate, edate = dates_from_args(start, end, today,
                                   default_start=DEFAULT_GRAPH_DURATION)
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)
    smoothed_iter = weight_data.smoothed_weight_iter(sdate,
                                                     edate,
                                                     gamma=user_info.gamma)
    template_values = {
      'user': users.get_current_user(),
      'user_info': user_info,
      'entries': list(smoothed_iter),
      'durations': ('All', '1y', '6m', '3m', '2m', '1m', '2w', '1w'),
    }
    path = template_path('mobile_data.html')
    return self.response.out.write(template.render(path, template_values))

class Data(RequestHandler):
  def _render(self, fileform=None, textform=None, successful_command=None):
    logging.debug("Data url: %r" % Data.get_url(implicit_args=True))
    if fileform is None:
      fileform = CSVFileForm()
    if textform is None:
      textform = CSVTextForm()

    template_values = {
      'user': users.get_current_user(),
      'fileform': fileform,
      'textform': textform,
      'success': bool(successful_command),
    }
    path = template_path('data.html')
    return self.response.out.write(template.render(path, template_values))

  def post(self, command=''):
    if command == 'file':
      # POST a file
      fileform = CSVFileForm(self.request)
      textform = CSVTextForm()  # for template output
      activeform = fileform
    elif command == 'text':
      # POST a textarea
      fileform = CSVFileForm()  # for template output
      textform = CSVTextForm(self.request)
      activeform = textform
    else:
      # This shouldn't be a 'post': redirect to the main data page
      logging.error("Invalid data command: %r", command)
      return self.redirect(Data.get_url(), permanent=True)

    if not activeform.is_valid():
      return self._render(fileform=fileform,
                          textform=textform)
    else:
      # Cleaned data is a date,weight pair iterator
      user_info = get_current_user_info()
      weight_data = WeightData(user_info)
      try:
        entries = list(activeform.clean_data['csvdata'])
        if entries:
          weight_data.batch_update(entries)
        else:
          raise forms.ValidationError("No valid entries specified")
      except forms.ValidationError, e:
        activeform.errors['csvdata'] = e.messages
        return self._render(fileform=fileform,
                            textform=textform)
      return self.redirect(Data.get_url())

  def get(self, command=''):
    return self._render(successful_command=command)

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

class MobileSettings(webapp.RequestHandler):
  def _render(self, user_info, form):
    template_values = {
      'user': users.get_current_user(),
      'index_url': '/index',
      'data_url': '/data',
      'form': form,
    }

    path = template_path('mobile_settings.html')
    self.response.out.write(template.render(path, template_values))

  def post(self):
    user_info = get_current_user_info()
    form = SettingsForm(self.request.POST)
    if not form.is_valid():
      # Errors?  Just render the form: the POST didn't do anything, so a
      # refresh would be expected to "retry"
      return self._render(user_info, form)
    else:
      # No errors, store the data
      user_info.scale_resolution = form.clean_data['scale_resolution']
      user_info.gamma = form.clean_data['gamma']
      user_info.put()

      # Send the user to the default front page after settings are altered.
      return self.redirect(MobileGraph.get_url(implicit_args=True))

  def get(self):
    user_info = get_current_user_info()
    form = SettingsForm(initial={'gamma': user_info.gamma,
                                 'scale_resolution': user_info.scale_resolution,
                                })
    return self._render(user_info, form)

class CsvDownload(RequestHandler):
  def get(self, start='', end=''):
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)

    today = datetime.date.today()
    sdate, edate = dates_from_args(start, end, today, default_start='all')

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
  def get(self):
    self.redirect(users.create_logout_url())

class MobileDefaultRoot(RequestHandler):
  def get(self):
    self.redirect(MobileGraph.get_url())

class DefaultRoot(RequestHandler):
  def get(self):
    self.redirect(Graph.get_url())

def main():
  template.register_template_library('templatestuff')
  # In order to allow for a bare "graph" url, we specify it multiple times.
  # You'll see that pattern repeated with other URLs.  This approach allows the
  # somewhat broken Django regex parser to figure out how to generate URLs for
  # {% url %} tags.  It is technically possible to do it with | entries inside
  # of parenthesized expressions, but this confuses Django (it thinks all
  # arguments are required when they aren't.
  application = webapp.WSGIApplication(
      [
        ('/m/graph', MobileGraph),
        ('/m/data', MobileData),
        ('/m/settings', MobileSettings),
        ('/m/logout', Logout),
        ('/m/?', MobileDefaultRoot),
        ('/graph/([^/]+)/([^/]+)/?', Graph),
        ('/graph/([^/]+)/?', Graph),
        ('/graph/?', Graph),
        # TODO: implement clearing of weight data
        # TODO: implement csrf protection
        ('/data/(text|file)', Data),
        ('/data/?', Data),
        ('/csv/([^/]+)/([^/]+)/?', CsvDownload),
        ('/csv/([^/]+)/?', CsvDownload),
        ('/csv/?', CsvDownload),
        ('/logout/?', Logout),
        ('/?', DefaultRoot),
        # TODO: add a default handler - 404
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
