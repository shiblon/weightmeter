import csv
import logging
from datetime import date, datetime, timedelta
from itertools import izip, count
from StringIO import StringIO

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

class FloatRangeSelect(forms.Select):
  def __init__(self, min_delta=5.0, max_delta=5.0, resolution=0.5,
               default_mid=95.0, reversed=False,
               dispfmt="%0.1f", labelfmt="%0.1f", attrs=None):
    self.min_delta = min_delta
    self.max_delta = max_delta
    self.resolution = resolution
    self.dispfmt = dispfmt
    self.labelfmt = labelfmt
    self.default_mid = default_mid
    self.reversed = reversed
    super(FloatRangeSelect, self).__init__(attrs)

  def render(self, name, value=None, attrs=None):
    # Construct the choices list on the fly.  "Value" is the current selection,
    # and the range will be constructed around it..
    if value is None:
      value = self.default_mid
    elif isinstance(value, basestring):
      value = float(value)

    # Construct choices of the given resolution and delta on either side of
    # value.
    choices = []
    v = value
    while v >= value - self.min_delta:
      choices.append((self.labelfmt % v, self.dispfmt % v))
      v -= self.resolution
    choices.reverse()
    v = value + self.resolution
    while v <= value + self.max_delta:
      choices.append((self.labelfmt % v, self.dispfmt % v))
      v += self.resolution
    if self.reversed:
      choices.reverse()

    return super(FloatRangeSelect, self).render(name, value, attrs, choices)

class FloatRangeSelectField(forms.ChoiceField):
  def __init__(self, min_delta=5.0, max_delta=5.0, resolution=0.5,
               default_mid=95.0, reversed=False,
               dispfmt="%0.1f", labelfmt="%0.1f",
               *args, **kargs):
    """Creates a ChoiceField, but makes it easier to specify float lists
    centered on a previously chosen value.
    """
    super(FloatRangeSelectField, self).__init__(
      widget=FloatRangeSelect(min_delta, max_delta, resolution, default_mid,
                              reversed, dispfmt, labelfmt, kargs.get('attrs')),
      *args, **kargs)

  def clean(self, value):
    # Try to convert it to a float
    try:
      return float(value)
    except ValueError:
      raise forms.ValidationError("Ensure this is a floating point number")

class DateSelect(forms.Select):
  def __init__(self, num_days=14, dispfmt="%a, %b %d",
               labelfmt="%Y-%m-%d", attrs=None):
    self.num_days = num_days
    self.dispfmt = dispfmt
    self.labelfmt = labelfmt
    super(DateSelect, self).__init__(attrs)

  def render(self, name, value, attrs=None):
    # Construct the choices list on the fly.  "Value" is the last date.
    if not value:
      value = date.today()
    elif isinstance(value, basestring):
      value = datetime.strptime(value, '%Y-%m-%d').date()

    # -1 so that we are inclusive of the initial value.  -2 just in case we
    # have a bad timezone interaction on the east side of GMT.
    start = value - timedelta(days=max(0, self.num_days - 2))
    dates = (start + timedelta(days=i) for i in xrange(self.num_days))
    choices = [(d.strftime(self.labelfmt), d.strftime(self.dispfmt))
               for d in dates]
    return super(DateSelect, self).render(name, value, attrs, choices)

class DateSelectField(forms.DateField):
  def __init__(self, num_days=14, dispfmt="%a, %b %d", labelfmt="%Y-%m-%d",
               *args, **kargs):
    super(DateSelectField, self).__init__(input_formats=[labelfmt],
                                          widget=DateSelect(num_days=num_days,
                                                            dispfmt=dispfmt,
                                                            labelfmt=labelfmt),
                                          *args, **kargs)

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

class FloatSelectField(forms.ChoiceField):
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
    super(FloatSelectField, self).__init__(choices, required, widget,
                                           label, initial, help_text)
    self.floatfmt = floatfmt
    self.float_choices = fc
    
  def clean(self, value):
    # Turn it into the right kind of string
    try:
      choice_value = self.floatfmt % float(value)
    except ValueError:
      raise forms.ValidationError("Ensure this is a floating point number")
    return float(super(FloatSelectField, self).clean(choice_value))

class CSVWeightField(forms.Field):
  widget = forms.FileInput

  def clean(self, value):
    """Outputs an iterator over date,weight pairs"""
    # Allow this to be used for file input or text input
    if hasattr(value, 'file'):
      data_file = value.file
    else:
      data_file = StringIO(value)

    # Try all different CSV formats, starting with comma-delimited.  Break if
    # one of them works on the first row, then assume that all other rows will
    # use that delimiter.
    for delimiter in (',', ' ', '\t'):
      logging.debug("CSV import: trying delimiter '%s'", delimiter)
      data_file.seek(0)
      reader = csv.reader(data_file, delimiter=delimiter)
      try:
        logging.debug("CSV import: trying a row")
        row = None
        while not row:
          try:
            row = reader.next()
          except StopIteration:
            raise forms.ValidationError("Empty csv entry")
          if not row or row[0].lstrip().startswith('#'):
            # Skip empty rows and comment rows
            continue
          elif len(row) == 1:
            # delimiter failed, or invalid format
            raise csv.Error("Invalid data for delimiter '%s'" % delimiter)
        # Found one that works, so create an iterator and pass it out
        def weight_csv_row_iter():
          data_file.seek(0)
          reader = csv.reader(data_file, delimiter=delimiter)
          for lineno, row in izip(count(1), reader):
            if not row or row[0].lstrip().startswith('#'):
              continue
            elif len(row) < 2:
              raise forms.ValidationError("Invalid entry at line %d: %r" %
                                          (lineno, row))
            else:
              datestr, weightstr = row[:2]

              if weightstr in ('-', '_', ''):
                weightstr = '-1'

              for dateformat in ('%m/%d/%Y', '%Y-%m-%d'):
                try:
                  date = datetime.strptime(datestr, dateformat).date()
                  break
                except ValueError:
                  pass
              else:
                raise forms.ValidationError("Invalid date at line %d: %r" %
                                            (lineno, datestr))

              try:
                weight = float(weightstr)
              except ValueError:
                raise forms.ValidationError("Invalid weight at line %d: %r" %
                                            (lineno, weightstr))

              # All's well: emit
              yield date, weight
        return weight_csv_row_iter()
      except csv.Error, e:
        logging.warn("CSV delimiter '%s' invalid for uploaded data", delimiter)
    else:
      data_file.seek(0)
      line = data_file.readline()
      data_file.close()
      logging.error("Unrecognized csv format: '%s'", line)
      raise forms.ValidationError("Unrecognized csv format: '%s'" % line)
