from __future__ import division

import csv
import datetime
import json
import logging
import math
import os
import os.path
import re
import webapp2

from StringIO import StringIO

from google.appengine.api import users
from google.appengine.ext import db

# This has to come after the appengine includes, otherwise the appropriate
# environment is not yet set up for Django and it barfs.
try:
  from django import newforms as forms
except ImportError:
  from django import forms

from django.template import Context
from django.template import loader

from util import forms as my_forms

# TODO
# - fix non-mobile site and launch version 2.0
# - implement clearing out of weight data
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
from util.forms import FloatSelectField
from util.forms import FloatRangeSelectField
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
  return name
  #return os.path.join(os.path.dirname(__file__), 'templates', name)

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

class WeightChoiceForm(forms.Form):
  date = DateSelectField()
  weight = FloatRangeSelectField(reversed=True)

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
  scale_resolution = FloatSelectField(
    initial=.5,
    floatfmt='%0.02f',
    float_choices=[.1, .2, .25, .5, 1.],
  )
  gamma = FloatSelectField(
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
  _default_form_style = 'text'

  def _form_style(self):
    return self.request.get('fs', self._default_form_style)

  def _alternate_form_style(self):
    fs = self._form_style()
    if fs == 'list':
      return 'text'
    else:
      return 'list'

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
      form = self._make_weight_form(initial={'weight': recent_weight,
                                             'date': today})

    afs = self._alternate_form_style()

    # Output to the template
    template_values = Context({
        'img': img,
        'user': users.get_current_user(),
        'form': form,
        'durations': DEFAULT_DURATIONS,
        'alternate_form_style': {
          'url': "/graph?fs=%s" % (afs,),
          'name': afs,
          },
        XSRF_TOKEN_NAME: self._xsrf_token,
        })

    t = loader.get_template(self._template_path())
    return self.response.write(str(t.render(template_values)))

  def _template_path(self):
    return template_path('index.html')

  def _on_success(self):
    return self.redirect("/graph")

  def _make_weight_form(self, *args, **kargs):
    form_style = self._form_style()
    if form_style == 'list':
      form = WeightChoiceForm(*args, **kargs)
      # Fill in the appropriate choice field parameters from user info
      user_info = kargs.get('user_info')
      if not user_info:
        user_info = get_current_user_info()
      form.fields['weight'].resolution = user_info.scale_resolution
    elif form_style == 'text':
      form = WeightEntryForm(*args, **kargs)
    else:
      raise ValueError("Invalid form style specified: %s" % form_style)

    return form

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

    form = self._make_weight_form(self.request.POST)
    logging.debug("POST data: %r", self.request.POST)
    if not form.is_valid():
      logging.debug("Invalid form")
      return self._render(user_info, form)
    else:
      logging.debug("valid form")
      date = form.cleaned_data['date']
      weight = form.cleaned_data['weight']
      weight_data.update(date, weight)
      return self._on_success()

class MobileGraph(Graph):
  _default_graph_width = DEFAULT_MOBILE_GRAPH_WIDTH
  _default_graph_height = DEFAULT_MOBILE_GRAPH_HEIGHT
  _default_form_style = 'list'

  def _template_path(self):
    return template_path('mobile_index.html')

  def _on_success(self):
    return self.redirect("/m/graph")

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
    template_values = Context({
      'user': users.get_current_user(),
      'user_info': user_info,
      'entries': list(smoothed_iter),
      'durations': DEFAULT_DURATIONS,
    })
    t = loader.get_template('mobile_data.html')
    return self.response.write(str(t.render(template_values)))

class ApiChartData(RequestHandler):
  def get(self):
    today = datetime.date.today()
    start = self.request.get('s', DEFAULT_GRAPH_DURATION)
    end = self.request.get('e', '')
    # Get the number of samples. A bit circular because we're converting from
    # string and trying to let 0 be special all at the same time (zero means
    # "all").
    samples = self.request.get('samples', None)
    if samples.lower() == 'none':
      samples = None
    if samples:
      samples = int(samples)
    if samples <= 0:
      samples = None
    sdate, edate = dates_from_args(start, end, today)
    user_info = get_current_user_info()
    weight_data = WeightData(user_info)
    smoothed_iter = weight_data.smoothed_weight_iter(sdate,
                                                     edate,
                                                     samples,
                                                     gamma=user_info.gamma)
    self.response.headers['Content-Type'] = 'application/json'
    obj = {
      'data': {
        'columns': ['Date', 'Weight', 'Smoothed'],
        'rows': list((str(d), w, s) for d, w, s in smoothed_iter),
      }
    }
    return self.response.write(json.dumps(obj))

class Data(RequestHandler):
  def _render(self, fileform=None, textform=None, successful_command=None):
    if fileform is None:
      # TODO: kill this. It doesn't work anymore. Uploads will have to be handled differently.
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
    template_values = Context({
      'user': users.get_current_user(),
      'fileform': fileform,
      'textform': textform,
      'success': bool(successful_command),
      'entries': list(smoothed_iter),
      'durations': DEFAULT_DURATIONS,
      XSRF_TOKEN_NAME: self._xsrf_token,
    })
    t = loader.get_template('data.html')
    return self.response.write(str(t.render(template_values)))

  def _add(self):
    submit_type = self.request.get('type')
    if submit_type == 'file':
      user_info = get_current_user_info()
      weight_data = WeightData(user_info)
      # NOTE: we can't use Django form validation anymore because it doesn't do
      # file uploads properly without a database. That's total overkill for us.
      csvdata = self.request.POST['csvdata']
      try:
        # Cleaned data is a date,weight pair iterator
        entries = list(my_forms.csv_row_iter(csvdata.file))
        if entries:
          weight_data.batch_update(entries)
        else:
          raise forms.ValidationError("No valid entries specified")
      except forms.ValidationError, e:
        fileform.errors['csvdata'] = e.messages
        return self._render(fileform=fileform, textform=textform)
    elif submit_type == 'text':
      # POST a textarea
      fileform = CSVFileForm()  # for template output
      textform = CSVTextForm(self.request)
      if not textform.is_valid():
        return self._render(fileform=fileform, textform=textform)
      # Cleaned data is a date,weight pair iterator
      user_info = get_current_user_info()
      weight_data = WeightData(user_info)
      try:
        entries = list(textform.cleaned_data['csvdata'])
        if entries:
          weight_data.batch_update(entries)
        else:
          raise forms.ValidationError("No valid entries specified")
      except forms.ValidationError, e:
        textform.errors['csvdata'] = e.messages
        return self._render(fileform=fileform, textform=textform)
    else:
      # Unknown data type: fail silently (mucking about with the form, eh?)
      logging.error("Invalid data submit type: %r", submit_type)
      return self.redirect("/data")

    return self.redirect("/data")

  def _delete(self):
    # TODO: implement deletion of all data
    return self.redirect("/data")

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
      return self.redirect("/data")

  @xsrf_aware('data', get_current_user_info)
  def get(self):
    return self._render()

class MobileSettings(webapp2.RequestHandler):
  def _render(self, user_info, form):
    template_values = Context({
      'user': users.get_current_user(),
      'index_url': '/index',
      'data_url': '/data',
      'form': form,
      XSRF_TOKEN_NAME: self._xsrf_token,
    })

    t = loader.get_template(self._template_path())
    return self.response.write(str(t.render(template_values)))

  def _template_path(self):
    return template_path('mobile_settings.html')

  def _on_success(self):
    return self.redirect("/m/graph")

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
      user_info.scale_resolution = form.cleaned_data['scale_resolution']
      user_info.gamma = form.cleaned_data['gamma']
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
    return self.redirect("/graph")

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
    self.redirect(users.create_logout_url("/graph"))

class MobileLogout(Logout):
  def get(self):
    self.redirect(users.create_logout_url("/m/graph"))

class MobileDefaultRoot(RequestHandler):
  def get(self):
    self.redirect("/m/graph")

class DefaultRoot(RequestHandler):
  def get(self):
    self.redirect("/graph")

# This needs to be in the global scope, as the application is now run by the appengine runtime, not called as a CGI script.
app = webapp2.WSGIApplication(
    routes=[
      (r'/m/graph', MobileGraph),
      (r'/m/data', MobileData),
      (r'/m/settings', MobileSettings),
      (r'/m/logout', MobileLogout),
      (r'/m/?', MobileDefaultRoot),
      (r'/api/chartdata', ApiChartData),
      (r'/graph', Graph),
      (r'/data', Data),
      (r'/csv', CsvDownload),
      (r'/settings', Settings),
      (r'/logout', Logout),
      (r'/?', DefaultRoot),
      # TODO: add a default handler - 404
    ],
    debug=True)
