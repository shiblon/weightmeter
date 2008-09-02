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

# TODO
# - fix non-mobile site and launch version 2.0
# - implement clearing out of weight data
# - implement csrf protection
# - rethink param sanitizers (make them a decorator, perhaps)
# - add pytz and use it for all references to "today"
# - make a user_info setting for the default duration
# - try a restful approach: give every "thing" a URL.  This includes users.
#   That way, we can have the user listed as part of the url, and can base
#   everything off of that.  Makes it very simple for an administrator to do
#   administrative tasks, and also allows for possible sharing in the future.

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
from util.xsrf import xsrf_aware
from util.xsrf import TOKEN_NAME as XSRF_TOKEN_NAME
# TODO: get rid of this - make param sanitizer its own thing in the util
# directory
from wsgiutil import ParamSanitizer

# Set constants
DEFAULT_SELECT_DAYS = 14
DEFAULT_GRAPH_DURATION = '2w'
DEFAULT_POUND_SELECTION = 2
DEFAULT_DURATIONS = ('All', '1y', '6m', '3m', '2m', '1m', '2w', '1w')

DEFAULT_ERROR_PATH = '/error'
DEFAULT_REDIR_PATH = '/index'

DEFAULT_MOBILE_GRAPH_WIDTH = 300
DEFAULT_MOBILE_GRAPH_HEIGHT = 200

DEFAULT_GRAPH_WIDTH = 600
DEFAULT_GRAPH_HEIGHT = 400

MAX_GRAPH_SAMPLES = 200

##############################################################################
# Functions
##############################################################################
def template_path(name):
  return os.path.join(os.path.dirname(__file__), 'templates', name)

def get_current_user_info():
  user = users.get_current_user()
  assert user is not None
  return UserInfo.get_or_insert('u:' + user.email(), user=user)

def chart_url(weight_data, width, height, start, end, gamma):
  cw, ch = chartserver_bounded_size(width, height)
  samples = min(MAX_GRAPH_SAMPLES, cw // 4)
  smoothed_iter = weight_data.smoothed_weight_iter(start, end, samples, gamma)
  return chartserver_weight_url(cw, ch, smoothed_iter)

##############################################################################
# Forms
##############################################################################

class WeightEntryForm(forms.Form):
  date = DateSelectField()
  weight = FloatField(max_length=7, widget=forms.TextInput(attrs={'size': 5}))

class CSVFileForm(forms.Form):
  csvdata = CSVWeightField(widget=forms.FileInput)

class CSVTextForm(forms.Form):
  csvdata = CSVWeightField(widget=forms.Textarea(attrs={
                                                 'rows': 8,
                                                 'cols': 30,
                                                 }))

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

##############################################################################
# Handlers
##############################################################################
class Graph(RequestHandler):
  _default_graph_width = DEFAULT_GRAPH_WIDTH
  _default_graph_height = DEFAULT_GRAPH_HEIGHT

  def _render(self, user_info, form=None):
    today = datetime.date.today()

    start = self.request.get('s', DEFAULT_GRAPH_DURATION)
    end = self.request.get('e', '')

    try:
      sdate, edate = dates_from_args(start, end, today)
    except ValueError:
      # Pass errors silently - just ignore the bad input
      sdate, edate = dates_from_args(DEFAULT_GRAPH_DURATION, 'today')

    sanitizer = ParamSanitizer(
      self.request,
      ('w', ParamSanitizer.Integer, self._default_graph_width),
      ('h', ParamSanitizer.Integer, self._default_graph_height),
      default_on_error=True)

    weight_data = WeightData(user_info)
    img_width = sanitizer.params['w']
    img_height = sanitizer.params['h']

    # Make a chart
    img = {
        'width': img_width,
        'height': img_height,
        'url': chart_url(weight_data,
                         img_width,
                         img_height,
                         sdate,
                         edate,
                         user_info.gamma)
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
        'durations': DEFAULT_DURATIONS,
        XSRF_TOKEN_NAME: self._xsrf_token,
        }

    path = self._template_path()
    return self.response.out.write(template.render(path, template_values))

  def _template_path(self):
    return template_path('graph.html')

  def _on_success(self):
    return self.redirect(Graph.get_url())

  @xsrf_aware('update_weight', get_current_user_info)
  def get(self):
    # Get the settings and info for this user
    user_info = get_current_user_info()
    self._render(user_info)

  @xsrf_aware('update_weight', get_current_user_info)
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
      return self._on_success()

class MobileGraph(Graph):
  _default_graph_width = DEFAULT_MOBILE_GRAPH_WIDTH
  _default_graph_height = DEFAULT_MOBILE_GRAPH_HEIGHT

  def _template_path(self):
    return template_path('mobile_index.html')

  def _on_success(self):
    return self.redirect(MobileGraph.get_url())

class MobileData(RequestHandler):
  def get(self):
    today = datetime.date.today()
    start = self.request.get('s', DEFAULT_GRAPH_DURATION)
    end = self.request.get('e', '')
    sdate, edate = dates_from_args(start, end, today)
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)
    smoothed_iter = weight_data.smoothed_weight_iter(sdate,
                                                     edate,
                                                     gamma=user_info.gamma)
    template_values = {
      'user': users.get_current_user(),
      'user_info': user_info,
      'entries': list(smoothed_iter),
      'durations': DEFAULT_DURATIONS,
    }
    path = template_path('mobile_data.html')
    return self.response.out.write(template.render(path, template_values))

class Data(RequestHandler):
  def _render(self, fileform=None, textform=None, successful_command=None):
    if fileform is None:
      fileform = CSVFileForm()
    if textform is None:
      textform = CSVTextForm()

    today = datetime.date.today()
    start = self.request.get('s', DEFAULT_GRAPH_DURATION)
    end = self.request.get('e', '')
    sdate, edate = dates_from_args(start, end, today)
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)
    smoothed_iter = weight_data.smoothed_weight_iter(sdate,
                                                     edate,
                                                     gamma=user_info.gamma)
    template_values = {
      'user': users.get_current_user(),
      'fileform': fileform,
      'textform': textform,
      'success': bool(successful_command),
      'entries': list(smoothed_iter),
      'durations': DEFAULT_DURATIONS,
      XSRF_TOKEN_NAME: self._xsrf_token,
    }
    path = template_path('data.html')
    return self.response.out.write(template.render(path, template_values))

  def _add(self):
    submit_type = self.request.get('type')
    if submit_type == 'file':
      # POST a file
      fileform = CSVFileForm(self.request)
      textform = CSVTextForm()  # for template output
      activeform = fileform
    elif submit_type == 'text':
      # POST a textarea
      fileform = CSVFileForm()  # for template output
      textform = CSVTextForm(self.request)
      activeform = textform
    else:
      # Unknown data type: fail silently (mucking about with the form, eh?)
      logging.error("Invalid data submit type: %r", submit_type)
      return self.redirect(Data.get_url())

    if not activeform.is_valid():
      return self._render(fileform=fileform, textform=textform)
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
        return self._render(fileform=fileform, textform=textform)
    return self.redirect(Data.get_url())

  def _delete(self):
    # TODO: implement deletion of all data
    return self.redirect(Data.get_url())

  @xsrf_aware('data', get_current_user_info)
  def post(self):
    cmd = self.request.get('cmd')
    if cmd == 'add':
      return self._add()
    elif cmd == 'delete':
      return self._delete()
    else:
      # fail silently - you can only get here by mucking about with urls
      logging.error("Invalid post command: %r", cmd)
      return self.redirect(Data.get_url())

  @xsrf_aware('data', get_current_user_info)
  def get(self):
    return self._render()

class MobileSettings(webapp.RequestHandler):
  def _render(self, user_info, form):
    template_values = {
      'user': users.get_current_user(),
      'index_url': '/index',
      'data_url': '/data',
      'form': form,
      XSRF_TOKEN_NAME: self._xsrf_token,
    }

    path = self._template_path()
    self.response.out.write(template.render(path, template_values))

  def _template_path(self):
    return template_path('mobile_settings.html')

  def _on_success(self):
    return self.redirect(MobileGraph.get_url())

  @xsrf_aware('data', get_current_user_info)
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
      return self._on_success()

  @xsrf_aware('data', get_current_user_info)
  def get(self):
    user_info = get_current_user_info()
    form = SettingsForm(initial={'gamma': user_info.gamma,
                                 'scale_resolution': user_info.scale_resolution,
                                })
    return self._render(user_info, form)

class Settings(MobileSettings):
  def _template_path(self):
    return template_path('settings.html')

  def _on_success(self):
    return self.redirect(Graph.get_url())

class CsvDownload(RequestHandler):
  def get(self):
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)

    today = datetime.date.today()

    start = self.request.get('s', 'all')
    end = self.request.get('e', '')
    sdate, edate = dates_from_args(start, end, today)

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
    self.redirect(users.create_logout_url(Graph.get_url()))

class MobileLogout(Logout):
  def get(self):
    self.redirect(users.create_logout_url(MobileGraph.get_url()))

class MobileDefaultRoot(RequestHandler):
  def get(self):
    self.redirect(MobileGraph.get_url())

class DefaultRoot(RequestHandler):
  def get(self):
    self.redirect(Graph.get_url())

##############################################################################
# Main
##############################################################################
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
        ('/m/logout', MobileLogout),
        ('/m/?', MobileDefaultRoot),
        ('/graph', Graph),
        ('/data', Data),
        ('/csv', CsvDownload),
        ('/settings', Settings),
        ('/logout', Logout),
        ('/?', DefaultRoot),
        # TODO: add a default handler - 404
      ],
      debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
