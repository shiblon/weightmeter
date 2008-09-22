from __future__ import division

import base64
import hashlib
import logging
import random
import time
import hmac

_DELIM = '|'
TOKEN_NAME = 'xsrftoken'
DEFAULT_EXPIRE_MICROS = 24 * 3600 * 1000000  # 1 full day

def make_secret():
  """Returns a 16-byte number as a long hex string"""
  r = random.Random()
  return ''.join(hex(r.randrange(0, 256))[2:] for i in xrange(16))

def make_xsrf_token(user_info, action, microseconds=None):
  """Returns base64(hmac(user_email DELIM action DELIM time) DELIM time)

  If microseconds is not specified, uses the current time.

  The secret is taken from user_info.  If no such secret exists, one is created
  and stored.
  """
  if microseconds is None:
    microseconds = int(time.time() * 1e6)

  interior = "%(email)s%(delim)s%(action)s%(delim)s%(time)s" % {
    'delim': _DELIM,
    'email': user_info.key(),
    'action': action,
    'time': str(microseconds),
  }

  if not user_info.xsrf_secret:
    user_info.xsrf_secret = make_secret()
    user_info.put()

  h = hmac.new(user_info.xsrf_secret, interior, hashlib.sha1).digest()
  h = base64.b64encode(h)
  return base64.b64encode("%s%s%s" % (h, _DELIM, microseconds))

def parse_xsrf_token(token):
  """Decodes then splits an xsrf token into hmac and time components."""
  h, t = base64.b64decode(token).split(_DELIM)
  return base64.b64decode(h), int(t)

class xsrf_aware(object):
  def __init__(self,
               action,
               user_info_factory,
               expire_micros=DEFAULT_EXPIRE_MICROS):
    """Creates an XSRF decorator that can wrap request methods.

    Params:
      action - the name of the action that is permitted
      user_info_factory - a callable that returns a UserInfo object
      expire_micros - number of microseconds that elapse before expiration
    """
    self.action = action
    self.user_info_factory = user_info_factory
    self.expire_micros = expire_micros

  def __call__(self, f):
    if f.__name__ not in ('post', 'get'):
      raise TypeError("Invalid function name for XSRF decorator: %r" % (
                      f.__name__,))

    def xsrf_wrapper(f_self, *args, **kargs):
      user_info = self.user_info_factory()
      if f.__name__ == 'post':
        # Look for the token in the self.request.POST, then validate it.
        token = f_self.request.POST.get(TOKEN_NAME)
        if not token:
          # TODO: make this nicer?
          raise ValueError("Failed to find XSRF token in form")

        h, t = parse_xsrf_token(token)
        cur_time = int(time.time() * 1e6)

        if cur_time - self.expire_micros > t:
          # TODO: make this nicer?
          raise ValueError("XSRF token expired")

        valid_token = make_xsrf_token(user_info, self.action, t)
        if token != valid_token:
          logging.error("Invalid token:\nexp %r\ngot %r",
                        valid_token, token)
          # TODO: nicer than an exception?
          raise ValueError("Invalid XSRF token")

      # Always create a new token in f_self so it can be injected into the
      # output stream by the rendering code in the handler.
      f_self._xsrf_token = make_xsrf_token(user_info, self.action)

      return f(f_self, *args, **kargs)

    xsrf_wrapper.__doc__ = f.__doc__
    xsrf_wrapper.__name__ = f.__name__
    return xsrf_wrapper
