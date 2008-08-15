"""wsgiutil.py

Utilities that make dealing with the WSGI webapp of Google's appengine a little
easier to deal with (it's already easy, but this still represents an
improvement.
"""

import logging
import re
from google.appengine.ext.webapp import RequestHandler
from urlparse import urlsplit, urlparse, urlunparse

class InvalidRequestURI(ValueError): pass

class InvalidParameter(ValueError):
  def __init__(self, value, *args, **kargs):
    super(InvalidParameter, self).__init__(*args)
    logging.error("InvalidParameter: %s" % self.message)
    self.value = value
    self.display = kargs.get('display')

class ErrorParams(object):
  def __init__(self, *params):
    """Accepts error information about parameters and stores it.

    Params:
      *params: each member is (name, badval, display_message)
    """
    self.params = {}
    self.messages = {}
    for name, badval, display in params:
      self.params[name] = badval
      self.messages[name] = display

  @classmethod
  def fromstring(cls, pstr):
    """Parses a piece of a query string that has error information."""
    param_list = []
    err_params = pstr.split('|')
    for param in err_params:
      param_parts = [unescape_query_parameter(x) for x in param.split(':')]
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
    for name, exception in self.failed.iteritems():
      e_name = escape_query_parameter(name)
      e_value = escape_query_parameter(exception.value, ':|')
      try:
        e_display = escape_query_parameter(exception.display, ':|')
      except AttributeError, e:
        display = 'invalid value'

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
        defaults = element[2:]
      assert 0 <= len(defaults) <= 1  # should be a single value

      val = self.request.get(param_name, *(str(x) for x in defaults))
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

  def param_string(self, overrides):
    """Create a parameter string from the sanitized parameters.

    Any parameter can be overridden in **kargs.

    params:
      overrides: dictionary of parameter overrides

    returns:
      parameter string (e.g., key=val&key2=val2)
    """
    pieces = []
    for key, value in overrides.iteritems():
      ekey = escape_query_parameter(key)
      evalue = escape_query_parameter(value)
      pieces.append("%s=%s" % (ekey, evalue))

    for key, value in self.params.iteritems():
      if key in overrides:
        continue
      ekey = escape_query_parameter(key)
      evalue = escape_query_parameter(value)
      pieces.append("%s=%s" % (ekey, evalue))
    pieces.sort()
    return "&".join(pieces)

  def redirect_string(self, path, overrides):
    """Create a redirect string from the path, the valid params, and overrides.

    Params:
      path - URL path of redirect
      overrides - dictionary of parameter overrides
    """
    return urlunparse(('', '', path, '', self.param_string(overrides), ''))

  def failed_redirect_url(self, *urls_to_try, **kargs):
    """Create a redirect URL from the first non-empty path in the list.

    Creates a URL path?query part from the first nonempty entry in
    urls_to_try.  It will look like this when finished:

      /original_path?original_query&error_query

    Note that only the path and query part of the url is used.  All else is
    ignored.

    Returns None on failure, so if you want to be sure to get something,
    include a default fallback as a parameter.

    params:
      *urls_to_try: just what it says
      error_name: name of the error parameter: defaults to 'E'

    returns:
      A redirect URL, or None if no non-empty path was found

    """
    error_name = kargs.get('error_name', 'E')
    for url in urls_to_try:
      if not url: continue
      path, query = urlsplit(url)
      if query and not query.endswith('&'):
        query += '&'
      query += error_name + '=' + self.failed_param_string()
      return urlunparse(('', '', path, '', query, ''))
    else:
      return None  # can't do anything else: nothing worked

  def failed_param_string(self):
    """Returns a parameter string that can be used to give helpful errors.

    Uses ErrorParams object to do its work.
    """
    ep = ErrorParams(
      *((n, e.value, e.display or '') for n, e in self.failed.iteritems()))
    return str(ep)

  @classmethod
  def ErrorParamString(cls, pstr):
    """Accepts error param strings as created by failed_param_string above.

    Returns:
      ErrorParam object
    """

  @classmethod
  def Date(cls, datestr):
    """Accepts YYYY/MM/DD or YYYY-MM-DD or YYYYMMDD - returns date object"""
    formats = ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d')
    datestr = datestr.strip()
    for format in formats:
      try:
        return datetime.datetime.strptime(datestr, format).date()
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
    return cls.Type(numstr, int, long, float, "Number required")

  @classmethod
  def Float(cls, numstr):
    """Accepts real numbers, returns a float"""
    return float(cls.Number(numstr))

  @classmethod
  def URIPath(cls, pathstr):
    """Accepts a URL, returns only the path and query (and fragment) parts"""
    scheme, netloc, path, params, query, fragment = urlparse(pathstr)
    if scheme or netloc:
      logging.warn("Path specified with scheme or domain set: %s" % pathstr)
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

class PathRequestHandler(RequestHandler):
  """A class that further simplifies the specification of webapp handlers.

  The main feature of this class is the ability to specify the path_regex as a
  class member.  The classmethod wsgi_handler can then be called to return an
  appropriate handler pair for the class.

  The regex is passed directly to the WSGIApplication class, so any groups
  within it are converted to function arguments for the various method
  handlers.

  Also, some convenience methods that are common among many request handlers
  are included here.

  Instead of overriding e.g. 'get' and 'post' instance methods, a user of this class might instead do the following:

  class MyHandler(PathRequestHandler):
    path_regex = r'^/m(/.*|)$'

    def GET(self, subpath):
      # Called when /m or /m/ are requested.  Redirects to the canonical root.
      self.redirect(self.add_to_request_path('/index'))

    def GET_index(self, subpath):
      # Called when /m/index is requested.
      self.response.out.write("We made it to the index!")
  """

  path_regex = None

  @classmethod
  def wsgi_handler(cls):
    if cls.path_regex is None:
      raise AttributeError("No path expression for " + cls.__name__)

    return cls.path_regex, cls

  def safe_redirect(self, uri):
    """Redirect, but throws an exception if the uri has a scheme or domain
    
    params:
      uri - relative path and query parameters
    """
    scheme, domain, path, query, fragments = urlparse(uri)
    if scheme or domain:
      raise InvalidRequestURI("Absolute URIs not allowed: %s" % uri)

    return self.redirect(uri)

  def add_to_request_path(self, suffix):
    """Adds a suffix to the request path, preserving query parameters"""
    # TODO: try using urlparse.urljoin here instead of all of this hand waving
    path = self.request.path
    if not path.endswith('/') and not suffix.startswith('/'):
      suffix = '/' + suffix
    elif path.endswith('/') and suffix.startswith('/'):
      path = path[:-1]

    query = self.request.query
    if query:
      query = '?' + query

    return path + suffix + query

  def _dispatch_func(self, method, *subparts):
    """Dispatches to a specialized GET or POST method, suffixed subpaths.
    
    The subpaths are optional.  There will be as many of them as there are
    groups in the path_regex.
    """
    path = "_".join(subparts).lower()
    logging.info("Handler %s: %s: %s" % (self.__class__.__name__, method, path))

    # Change / to _ and dispatch
    call_path = path.replace('/', '_')
    if not call_path.startswith('_'):
      call_path = '_' + call_path

    while call_path.endswith('_'):
      call_path = call_path[:-1]

    logging.info("call path " + call_path)

    func_name = method.upper() + call_path
    logging.info(func_name)

    func = getattr(self, func_name, None)
    if func is not None:
      return func(*subparts)
    else:
      raise ValueError("Unrecognized path: " + self.request.path)

  def get(self, *subparts):
    return self._dispatch_func('GET', *subparts)

  def post(self, *subparts):
    return self._dispatch_func('POST', *subparts)

def unescape_query_parameter(param):
  pattern = r'%([0-9a-fA-F]{2})'
  return re.sub(pattern, lambda m: chr(int(m.group(1), 16)), param)

def escape_query_parameter(param, to_escape=''):
  """Returns an escaped version of a failure param.

  params:
    param - parameter to escape
    to_escape - characters that will be escaped - in order

  Before escaping, the to_escape string is prepended with '%#&=;'
  """
  pattern = r'[%s]' % re.escape(r'%#&=' + to_escape)
  # the hex function prepends everything with 0x, so we remove that (hence
  # the [2:] below) before prepending '%'
  return re.sub(pattern, lambda m: '%' + hex(ord(m.group(0)))[2:], str(param))