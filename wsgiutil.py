"""wsgiutil.py

Utilities that make dealing with the WSGI webapp of Google's appengine a little
easier to deal with (it's already easy, but this still represents an
improvement.
"""

import logging
import re
from cgi import parse_qs
from datetime import datetime
from urlparse import urlsplit, urlparse, urlunparse

class InvalidRequestURI(ValueError): pass

class InvalidParameter(ValueError):
  def __init__(self, value, *args, **kargs):
    super(InvalidParameter, self).__init__(*args)
    self.value = value
    display = kargs.get('display', '')
    self.display = display or self.message
    logging.error("InvalidParameter: %s, %s", self.message, display)

  def __repr__(self):
    s = super(InvalidParameter, self).__repr__()
    return s + ' value: %r display: %r' % (self.value, self.display)

class ErrorParams(object):
  class Param(object):
    def __init__(self, name, badval, display):
      self.name = name
      self.badval = badval
      self.display = display

  def __init__(self, *params):
    """Accepts error information about parameters and stores it.

    Params:
      *params: each member is (name, badval, display_message)
    """
    self.params = {}
    for name, badval, display in params:
      self.params[name] = self.Param(name, badval, display)

  @classmethod
  def fromstring(cls, pstr):
    """Parses a piece of a query string that has error information."""
    param_list = []
    if pstr:
      err_params = pstr.split('|')
      for param in err_params:
        param_parts = [unescape_qp(x) for x in param.split(':')]
        try:
          name, val, msg = param_parts
        except ValueError, e:
          name, val = param_parts
          msg = ''
        param_list.append((name, val, msg))
    return cls(*param_list)

  def param_string(self):
    """Returns a parameter string that can be used to give helpful errors.

    This creates a parameter string with information about the form field
    problems so that they can be passed on to the user in a helpful format,
    e.g., as red text next to the broken field value.

    The string has the following form: <name>:<val>:<msg>|<name>:<val>:<msg>
    Since the characters : and | have special meaning, they will be % escaped
    before that escaping is performed on the URI path.  Thus, after unescaping
    is performed once on the whole, it will need to be performed once more on
    each individual part after splitting.

    Characters escaped (in addition to standard query specials) are ':|'
    """
    pieces = []
    for name, param in self.params.iteritems():
      e_name = escape_qp(param.name)
      e_value = escape_qp(param.badval, ':|')
      e_display = escape_qp(param.display or 'invalid value', ':|')
      pieces.append('%s:%s:%s' % (e_name, e_value, e_display))
    pieces.sort()
    return "|".join(pieces)

  __str__ = param_string

class ParamSanitizer(object):
  """Provides an easy way to validate/sanitize form entry syntax and bounds.

  Create an instance of this class with a list of pairs: (name, validator)
  Several standard validators are provided for convenience.
  """

  def __init__(self, request, *elements, **kargs):
    """Create a form validator and validate the elements in the request.

    The sanitizer callable should accept a single value and return the
    sanitized version of that value.  It must throw an InvalidParameter
    exception if the value cannot be sanitized.

    When finished, the object will have members "params", "failed", and
    "originals":

      'params' is a dictionary param_name:sanitized_value
      'failed' is a dictionary param_name:exception
      'originals' is a dictionary param_name:original_str

    params:
      request - A WSGI request object
      *elements - A list of (<form_name>, <sanitizer_callable>, [default val])
      default_on_error (False) - use the default when a sanitization
          error occurs, in addition to using it to specify missing parameters.
          Note that errors are still failures when no default is specified.
    """
    self.request = request
    self.elements = elements

    # Validate all elements.  If an InvalidParameter exception is thrown, store
    # the exception, flag as invalid, and remember it.  All other exceptions
    # pass through.

    params = self.params = {}
    failed = self.failed = {}
    originals = self.originals = {}

    for element in elements:
      assert 2 <= len(element) <= 3, "Elements must be pairs or triples"
      param_name, sanitizer = element[:2]

      # Use a list here to make it easier to allow None as a valid value
      defaults = []
      if len(element) > 2:
        defaults = [str(element[2])]

      val = self.request.get(param_name, *defaults)
      originals[param_name] = val
      try:
        sanitized_val = sanitizer(val)
        params[param_name] = sanitized_val
      except InvalidParameter, e:
        # If we are allowed to silently declare success with a suitable default
        # value, we do that.
        if kargs.get('default_on_error', False) and defaults:
          params[param_name] = defaults[0]
        else:
          failed[param_name] = e

  def success(self):
    return not self.failed

  def failure(self):
    return not not self.failed

  def param_string(self, overrides=None):
    """Create a parameter string from the sanitized parameters.

    Any parameter can be overridden in **kargs.

    params:
      overrides: dictionary of parameter overrides

    returns:
      parameter string (e.g., key=val&key2=val2)
    """
    pieces = []
    if overrides is None:
      overrides = {}

    for key, value in overrides.iteritems():
      ekey = escape_qp(key)
      evalue = escape_qp(value)
      pieces.append("%s=%s" % (ekey, evalue))

    for key, value in self.params.iteritems():
      if key in overrides:
        continue
      ekey = escape_qp(key)
      evalue = escape_qp(value)
      pieces.append("%s=%s" % (ekey, evalue))
    pieces.sort()
    return "&".join(pieces)

  def redirect_string(self, path, overrides=None):
    """Create a redirect string from the path, the valid params, and overrides.

    Params:
      path - URL path of redirect
      overrides - dictionary of parameter overrides
    """
    return urlunparse(('', '', path, '', self.param_string(overrides), ''))

  def failed_redirect_url(self, url, **kargs):
    """Create a redirect URL from the path.

    Creates a URL path?query part from the url.  It will look like this when
    finished:

      /original_path?original_query&error_query

    Note that only the path and query part of the url is used.  All else is
    ignored.

    Returns None on failure, so if you want to be sure to get something,
    include a default fallback as a parameter.

    params:
      url: just what it says
      error_name: name of the error parameter: defaults to 'E'

    returns:
      A redirect URL, or None if no non-empty path was found

    """
    assert url, "Cannot redirect to empty URL"
    error_name = kargs.get('error_name', 'E')
    path, query = urlsplit(url)[2:4]
    if query and not query.endswith('&'):
      query += '&'
    query += error_name + '=' + self.failed_param_string()
    return urlunparse(('', '', path, '', query, ''))

  def failed_param_string(self):
    """Returns a parameter string that can be used to give helpful errors.

    Uses ErrorParams object to do its work.
    """
    ep = ErrorParams(
      *((n, e.value, e.display) for n, e in self.failed.iteritems()))
    return str(ep)

  @classmethod
  def Date(cls, datestr):
    """Accepts YYYY/MM/DD or YYYY-MM-DD or YYYYMMDD - returns date object"""
    formats = ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d')
    datestr = datestr.strip()
    for format in formats:
      try:
        return datetime.strptime(datestr, format).date()
      except ValueError, e:
        pass
    else:
      raise InvalidParameter(
          datestr,
          "Unrecognized date format: %s" % datestr,
          display="Invalid date")

  @classmethod
  def Type(cls, valstr, *converters, **kargs):
    """Accepts numbers that can be coerced to int, float, or other types.

    params:
      valstr - The string to be converted
      converters - List of conversion callables, to be tried in order
      message_format - (keyword arg) Message format to use in the exception
      display - (keyword arg) Message to display on failure

    returns:
      number type (typically int, long, or float)
    """
    valstr = valstr.strip()
    for converter in converters:
      try:
        return converter(valstr)
      except (ValueError, OverflowError), e:
        pass
    else:
      message_format = kargs.get('message_format', 'Type: unable to convert %s')
      display = kargs.get('display', 'Invalid field value')
      raise InvalidParameter(valstr, message_format % valstr, display=display)

  @classmethod
  def Integer(cls, numstr):
    """Accepts integer values"""
    return cls.Type(numstr, int, long, display="Integer required")

  @classmethod
  def Number(cls, numstr):
    """Accepts real numbers (int, float, but not complex)"""
    return cls.Type(numstr, int, long, float, display="Number required")

  @classmethod
  def Float(cls, numstr):
    """Accepts real numbers, returns a float"""
    return float(cls.Number(numstr))

  @classmethod
  def URIPath(cls, pathstr):
    """Accepts a URL, returns only the path and query (and fragment) parts"""
    scheme, netloc, path, params, query, fragment = urlparse(pathstr)
    if scheme or netloc:
      logging.warn("Path specified with scheme or domain set: %s", pathstr)
    # TODO: detect invalid urls?
    return urlunparse(['', '', path, params, query, fragment])

  @classmethod
  def ErrorParams(cls, estr):
    try:
      return ErrorParams.fromstring(estr)
    except ValueError, e:
      raise InvalidParameter(estr, "Invalid error param: %s" % estr,
                             display="Bad error indicator")

  class Enumeration(object):
    def __init__(self, enumset):
      self.enums = set(str(s).lower() for s in enumset)

    def __call__(self, enumstr):
      if enumstr.lower() in self.enums:
        return enumstr
      raise InvalidParameter(enumstr,
                             "Invalid enumerated value: '%s'" % enumstr,
                             "Invalid choice")

def unescape_qp(param):
  pattern = r'%([0-9a-fA-F]{2})'
  return re.sub(pattern, lambda m: chr(int(m.group(1), 16)), param)

def escape_qp(param, to_escape=''):
  """Returns an escaped version of a failure param.

  params:
    param - parameter to escape
    to_escape - characters that will be escaped - in order

  Before escaping, the to_escape string is prepended with '%#?&=+;'
  """
  pattern = r'[%s]' % re.escape(r'%#?&=+;' + to_escape)
  # the hex function prepends everything with 0x, so we remove that (hence
  # the [2:] below) before prepending '%'
  return re.sub(pattern, lambda m: '%' + hex(ord(m.group(0)))[2:], str(param))

def param_str(params):
  """Create a parameter string from the dictionary of parameters"""
  paramstrs = []
  for k, v in params.iteritems():
    paramstrs.append("%s=%s" % (escape_qp(k), escape_qp(v)))
  return "&".join(paramstrs)
