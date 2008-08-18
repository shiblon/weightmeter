import logging
from datetime import date, timedelta

try:
  from django import newforms as forms
except ImportError:
  from django import forms

class FloatSelect(forms.Select):
  def __init__(self, floatfmt='%0.01f', *args, **kargs):
    self.floatfmt = floatfmt
    super(FloatSelect, self).__init__(*args, **kargs)

  def render(self, name, value, *args, **kargs):
    value = self.floatfmt % float(value)
    logging.debug("float select value: %r=%r", name, value)
    return super(FloatSelect, self).render(name, value, *args, **kargs)

class DateSelect(forms.Select):
  def __init__(self, num_days=14, dispfmt="%a, %b %d", labelfmt="%Y-%m-%d",
               attrs=None):
    self.num_days = num_days
    self.dispfmt = dispfmt
    self.labelfmt = labelfmt
    super(DateSelect, self).__init__(attrs)

  def render(self, name, value, attrs=None):
    # Construct the choices list on the fly.  "Value" is the last date.
    if not value:
      value = date.today()
    elif isinstance(value, basestring):
      value = datetime.strptime('%Y-%m-%d').date()

    # -1 so that we are inclusive of the initial value.  -2 just in case we
    # have a bad timezone interaction on the east side of GMT.
    start = value - timedelta(days=max(0, self.num_days - 2))
    dates = (start + timedelta(days=i) for i in xrange(self.num_days))
    choices = [(d.strftime(self.labelfmt), d.strftime(self.dispfmt))
               for d in dates]
    return super(DateSelect, self).render(name, value, attrs, choices)

class FloatField(forms.CharField):
  def __init__(self, floatfmt="%0.02f", max_length=None, min_length=None,
               required=True, widget=None, label=None, initial=None,
               help_text=None):
    if initial is not None:
      initial = floatfmt % float(inital)

    super(FloatField, self).__init__(max_length, min_length, required,
                                     widget, label, initial, help_text)

  def clean(self, value):
    cleaned = super(FloatField, self).clean(value)
    # Now try to convert it to a float
    try:
      return float(cleaned)
    except ValueError:
      raise forms.ValidationError("Ensure this is a floating point number")

class FloatChoiceField(forms.ChoiceField):
  def __init__(self, floatfmt='%0.01f', float_choices=(), choices=(),
               required=True, widget=None, label=None,
               initial=None, help_text=None):
    """Creates a ChoiceField, but makes it easier to specify float lists.

    If choices is present, float_choices will be ignored.  Otherwise it will be
    used (along with floatfmt) to generate an appropriate choices list.
    """
    if widget is None:
      widget = FloatSelect(floatfmt=floatfmt)

    if initial is not None:
      initial = floatfmt % float(initial)

    if float_choices and not choices:
      fc = (float(x) for x in float_choices)
      choices = [(floatfmt % c, floatfmt % c) for c in fc]
    super(FloatChoiceField, self).__init__(choices, required, widget,
                                           label, initial, help_text)
    self.floatfmt = floatfmt
    self.float_choices = fc
    
  def clean(self, value):
    # Turn it into the right kind of string
    try:
      choice_value = self.floatfmt % float(value)
    except ValueError:
      raise forms.ValidationError("Ensure this is a floating point number")
    return float(super(FloatChoiceField, self).clean(choice_value))
