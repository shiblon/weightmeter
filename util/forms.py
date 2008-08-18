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
    return super(FloatSelect, self).render(name, value, *args, **kargs)

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
    choice_value = self.floatfmt % float(value)
    return float(super(FloatChoiceField, self).clean(choice_value))
