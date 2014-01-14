import logging
from cgi import parse_qs
from urlparse import urlsplit, urlunparse

from google.appengine.ext.webapp import RequestHandler

class RequestHandler(RequestHandler):
  """A class that adds some nifty little features to webapp handlers.

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
  default_error_path = ''
  default_redir_path = ''

  @classmethod
  def wsgi_handler(cls):
    if cls.path_regex is None:
      raise AttributeError("No path expression for " + cls.__name__)

    return cls.path_regex, cls

  def safe_redirect(self, uri, permanent=False, **kargs):
    """Redirect, but throws an exception if the uri has a scheme or domain
    
    params:
      uri - relative path and query parameters
      overrides (keyword only): a dictionary of parameter overrides - a value
          of None indicates that the parameter should be removed.
    """
    if not uri:
      raise InvalidRequestURI("Empty URI specified");

    scheme, domain, path, query, fragments = urlsplit(uri)
    if scheme or domain:
      raise InvalidRequestURI("Absolute URIs not allowed: %s" % uri)

    # Parse the query, add overrides if there are any
    overrides = kargs.get('overrides', None)
    if overrides:
      query_dict = parse_qs(query)
      for k, v in overrides.iteritems():
        if k in query_dict and v is None:
            del query_dict[k]
        else:
          query_dict[escape_qp(k)] = escape_qp(v)
      params = []
      for k, v in query_dict.iteritems():
        # Handle the possibility of an iterable (repeated parameters)
        pieces = v
        if isinstance(v, basestring):
          pieces = [v]

        # Add them all
        for item in pieces:
          params.append("%s=%s" % (k, item))
      query = "&".join(params)

    uri = urlunparse(('', '', path, '', query, ''))

    logging.debug("safe redirect to %s", uri)
    return self.redirect(uri, permanent=permanent)

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
    logging.info("Handler %s: %s: %s", self.__class__.__name__, method, path)

    # Change / to _ and dispatch
    call_path = path.replace('/', '_')
    if not call_path.startswith('_'):
      call_path = '_' + call_path

    while call_path.endswith('_'):
      call_path = call_path[:-1]

    logging.info("call path %s", call_path)

    func_name = method.upper() + call_path
    logging.info("%s", func_name)

    func = getattr(self, func_name, None)
    if func is not None:
      return func(*subparts)
    else:
      raise ValueError("Unrecognized path: " + self.request.path)

  def get(self, *subparts):
    return self._dispatch_func('GET', *subparts)

  def post(self, *subparts):
    return self._dispatch_func('POST', *subparts)

