from __future__ import division

import datetime
import logging
import re

class DateDelta(object):
  """Creates a delta object that can be applied to any date object.

  Has human-friendly semantics, in the sense that -1 month is not necessarily
  -30 days, etc.
  """

  _rel_path_re = re.compile(r"^([-+]?\d+y)?([-+]?\d+m)?"
                            r"([-+]?\d+w)?([-+]?\d+d)?"
                            r"_?((last[-_]?|next[-_]?|nearest[-_]?|[-+]?\d+)?"
                            r"((s|su|sun|sundays?)|"
                            r"(m|mon|mondays?)|"
                            r"(t|tu|tue|tuesdays?)|"
                            r"(w|wed|wednesdays?)|"
                            r"(th|thu|thursdays?)|"
                            r"(f|fri|fridays?)|"
                            r"(sa|sat|saturdays?)))?$")

  _month_days = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

  def __init__(self,
      years=None, months=None, weeks=None, days=None,
      to_weekday=None, weekday_weeks=None):
    """Generate a time delta from a set of different kinds of delta.

    All parameters can be either negative or positive values.  Negative values
    search backwards in time.

    Params:
      years - number of years (equivalent to 12-month intervals)
      months - number of months
      weeks - number of weeks (equivalent to 7-day intervals)
      days - number of days
      to_weekday - int in [0..6]: go to the closest given weekday (0 = Sunday)
      weekday_weeks - how many to_weekday weekdays to skip
          0 means "go to the nearest in any direction", including today
          -N means e.g., N Sundays ago, excluding today if it's Sunday
          +N or N means e.g., N Sundays in the future, excluding today
          Default is 1 (+1)

    If both to_weekday and something else are specified, the weekday delta is
    evaluated last when applied to a particular date.  Thus,

      DateDelta(years=-2, to_weekday=3)

    produces the Wednesday after the date 2 years in the past, and

      DateDelta(years=-2, days=13, to_weekday=4, weekday_weeks=-2)

    produces the date two wednesdays before 13 days after 2 years ago.

    The year and month values are "fuzzy", meaning that they don't just
    translate to a particular number of days: if you ask to go back one month
    from July 31, it will yield the value June 30.  Similarly, if you try to go
    forward one month from January 31, it will yield February 28 or 29,
    depending on whether it is a leap year.
    """
    # 0 is a safe do-nothing value for these
    self.years = int(years or 0)
    self.months = int(months or 0)
    self.weeks = int(weeks or 0)
    self.days = int(days or 0)

    # Every integer means something here, so we have to allow None
    if to_weekday not in (None, ''):
      self.to_weekday = int(to_weekday) % 7
      try:
        self.weekday_weeks = int(weekday_weeks)
      except TypeError, e:
        self.weekday_weeks = 1
    else:
      self.to_weekday = None
      self.weekday_weeks = None
  
  @classmethod
  def fromstring(cls, deltastr, today=None):
    """Generate a delta from a string.

    The string has several components, all optional:
      "<n>y<n>m<n>w<n>d<weekday_spec>"
    where <n> is an integer, positive or negative.
    The <weekday_spec> part specifies a weekday, e.g.,
      "+1s" - means "one Sunday in the future"
      "-1s" - means "one Sunday in the past"
      "-2m" - means "two Mondays ago" (beware of ambiguity with months - if you
          only specify weekdays, use a longer form like "mon", "monday", or
          "mondays", otherwise it will get parsed as a month)
      "0s" - means "the nearest Sunday to this date, past or future"
      "w" - means "next wednesday" (again, beware of ambiguity with weeks -
          prefer the longer form of "wed", "wednesday", or "wednesdays")
      "next-th" means "the following Thursday".

    All of the following weekday specs are accepted:
      -5s
      +1sunday
      2sun
      nextmon
      last-mon
      nearest_wednesday

    You can see various combinations of allowable strings in that example.  The
    regular expression at the top of the class definition is the ultimate
    authority on what's allowed.

    Params:
      deltastr - string to parse
      today - today's date (a datetime.date object)

    >>> DateDelta.fromstring("4w")
    DateDelta(0, 0, 4, 0, None, None)
    >>> DateDelta.fromstring("mon")
    DateDelta(0, 0, 0, 0, 1, 1)
    >>> DateDelta.fromstring("4wed")
    DateDelta(0, 0, 0, 0, 3, 4)
    >>> DateDelta.fromstring("4w2w")
    DateDelta(0, 0, 4, 0, 3, 2)
    >>> DateDelta.fromstring("-10m-4dlast-monday")
    DateDelta(0, -10, 0, -4, 1, -1)
    >>> DateDelta.fromstring("+5y-2m_nearest_wed")
    DateDelta(5, -2, 0, 0, 3, 0)
    >>> DateDelta.fromstring("+5y-2mm")
    DateDelta(5, -2, 0, 0, 1, 1)
    >>> DateDelta.fromstring("")
    DateDelta(0, 0, 0, 0, None, None)
    """
    if today is None:
      today = datetime.date.today()

    deltastr = deltastr.lower()

    rel_match = cls._rel_path_re.match(deltastr)
    if not rel_match:
      raise ValueError("Invalid Date Delta '%s'" % deltastr)

    groups = [('' if x is None else x) for x in rel_match.groups()]
    # Get the easy stuff first: y, m, w, d are the first groups
    y, m, w, d = [int(x[:-1] or 0) for x in groups[:4]]

    weekday_weeks = None
    weekday = None
    if groups[4]:  # The weekday is specified
      # if it is a word, convert to a number, else assume an int
      wws = groups[5].strip('-_') or 'next'
      weekday_weeks = {'last': -1, 'next': +1, 'nearest': 0}.get(wws)
      if weekday_weeks is None:
        weekday_weeks = int(wws)

      # Find the first matching group index: that's the weekday.
      weekday = [i for i, x in enumerate(groups[7:]) if x][0]

    return cls(years=y, months=m, weeks=w, days=d,
               to_weekday=weekday, weekday_weeks=weekday_weeks)

  def copy(self):
    cls = self.__class__
    return cls(self.years, self.months, self.weeks, self.days,
               self.to_weekday, self.weekday_weeks)

  def inverted(self):
    """Return an inverted copy of this object.

    >>> DateDelta.fromstring("-10m+1w-4d_lasttue").inverted()
    DateDelta(0, 10, -1, 4, 2, 1)
    """
    c = self.copy()
    c.invert()
    return c

  def invert(self):
    """Invert the direction of delta in place.

    This reverses every individual component, which may not be the same as
    marching the same number of days in the reverse direction.

    >>> d = DateDelta.fromstring("-10m-4dlast-monday")
    >>> d
    DateDelta(0, -10, 0, -4, 1, -1)
    >>> d.invert()
    >>> d
    DateDelta(0, 10, 0, 4, 1, 1)
    """
    self.years = -self.years
    self.months = -self.months
    self.weeks = -self.weeks
    self.days = -self.days
    # self.to_weekday stays the same
    if self.weekday_weeks is not None:
      self.weekday_weeks = -self.weekday_weeks

  def add_to_date(self, date):
    """Advances to the appropriate date, given the delta specified.

    See docs for __init__

    Params:
      date - the datetime.date object used as a starting point

    Returns:
      datetime.date

    You can always think of this as traveling a certain distance from the
    starting date.  First travel the right number of months, then the right
    number of days, then advance to the nearest weekday specified.

    >>> DateDelta(months=-4).add_to_date(datetime.date(2008, 8, 31))
    datetime.date(2008, 4, 30)
    >>> DateDelta(years=-6, months=-6).add_to_date(datetime.date(2008, 8, 31))
    datetime.date(2002, 2, 28)
    >>> DateDelta(4, 2, 1, -1).add_to_date(datetime.date(2008, 5, 20))
    datetime.date(2012, 7, 26)
    """
    months = self.years * 12 + self.months
    days = self.weeks * 7 + self.days

    year = date.year
    month = date.month
    day = date.day

    new_month = (month - 1 + months) % 12 + 1
    new_year = year + (months // 12)
    # This elegant approach works out, believe it or not.  With floor
    # division, -13 // 12 = -2, and 13 // 12 = 1.  In both cases, if the new
    # month is smaller than the old month, this means we add one to the year.
    # Conceptually, smaller going backwards means we overstepped, and smaller
    # going forwards means we wrapped around.  Kind of neat.
    if new_month < month:
      new_year += 1

    # Test that we have the right number of days for this month.  We employ the
    # most liberal test for February (29 days), and if it passes with 29,
    # we try to create the datetime object and see what happens.
    allowed_days = self._month_days[new_month-1]
    if day > allowed_days:
      day = allowed_days

    if new_month == 2 and day == 29:
      # February is weird because of leap years - fall back on datetime to find
      # out whether this year works.
      try:
        new_date = datetime.date(new_year, new_month, day)
      except ValueError, e:
        if "day is out of range" in e.message:
          day = 28
        else:
          raise

    new_date = (datetime.date(new_year, new_month, day) +
                datetime.timedelta(days=days))

    # Now advance to the nearest weekday, if specified.
    # Behavior of weekday_weeks:
    # * 0 - go to nearest in any direction
    # * positive - go forward N of this weekday
    # * negative - go backward N of this weekday
    # If N=weekday_weeks is nonzero, the nearest weekday in the appropriate
    # direction is found, and then abs(N)-1 weeks are added or subtracted as
    # necessary
    if self.to_weekday is not None:
      cur_weekday = new_date.weekday()
      forward = (self.to_weekday - cur_weekday) % 7
      backward = (self.to_weekday - cur_weekday) % -7
      if self.weekday_weeks == 0:
        advance_days = forward if abs(forward) < abs(backward) else backward
      else:
        # Non-inclusive non-zero values mean that we have to treat 0-day
        # advancement specially (advance one more week in the right direction)
        if self.weekday_weeks < 0:
          advance_days = backward
          if advance_days == 0:
            advance_days += self.weekday_weeks * 7
          else:
            advance_days += (self.weekday_weeks + 1) * 7
        else:
          advance_days = forward
          if advance_days == 0:
            advance_days += self.weekday_weeks * 7
          else:
            advance_days += (self.weekday_weeks - 1) * 7
      new_date = new_date + datetime.timedelta(days=advance_days)

    return new_date

  def __repr__(self):
    """Representation (this is most likely eval-able)

    >>> repr(DateDelta(years=5))
    'DateDelta(5, 0, 0, 0, None, None)'
    >>> repr(DateDelta(years=5, months=-2, to_weekday=4))
    'DateDelta(5, -2, 0, 0, 4, 1)'
    """

    return "%s(%r, %r, %r, %r, %r, %r)" % (
        self.__class__.__name__,
        self.years,
        self.months,
        self.weeks,
        self.days,
        self.to_weekday,
        self.weekday_weeks,)

  __str__ = __repr__

def try_to_parse_absolute_date(datestr):
  """Attempt various simple date parses, accept first that works.

  Returns None on failure, a datetime.date on success.

  >>> try_to_parse_absolute_date('2007-06-10')
  datetime.date(2007, 6, 10)
  >>> try_to_parse_absolute_date('help me')
  >>> try_to_parse_absolute_date('200651')
  datetime.date(2006, 5, 1)
  >>> try_to_parse_absolute_date('1999_5_2')
  datetime.date(1999, 5, 2)
  """

  for format in ("%Y-%m-%d", "%Y_%m_%d", "%Y%m%d"):
    try:
      d = datetime.datetime.strptime(datestr, format).date()
      return d
    except ValueError, e:
      pass
  else:
    return None

def dates_from_path(spath, epath=None, today=None, default_start='2w'):
  """
  Params:
    spath - start date path component
    epath - end date path component
    today - datetime (or date) object representing today
    default_start - default spath value if no spath is specified.

  Returns:
    (start_date, end_date): datetime.date objects

  A note about the path components.  They specify the time range of interest.
  Why path components?  Because the range of times shown on the graph are
  pretty fundamental to the very concept of the graph.  The idea is to make it
  as intuitive as possible to specify the time period of interest.

  Either or both components can be empty or None.

  If either component starts with '/', it is removed before further processing
  occurs.

  The date range can be specified in many different ways.

  Two dates:
    /2008-08-04/2008-08-11
    /2008_08_04/2008_08_11
    /20080804/20080811

    The processing of the dates (for one or two) is attempted by strptime, in
    the order shown.

    The leftmost date is taken to be the start date, and the rightmost is the
    end date.

  One date:
    /20080804

    In this case, the date is taken to be the start date.  Since the second
    date is omitted, it is assumed to be "today".

  Instead of dates, one may specify relative times as allowed by the
  accompanying datedelta module's DateDelta class.

  This is most sensible when an absolute date is specified in concert with a
  relative indicator, such as /2008-08-04/4w (the four weeks following the
  4th of August 2008) or /2w/2008-08-04 (the two weeks preceding the given
  date).

  As with absolute dates, if no second date is given, it is assumed to be
  "today", with the obvious meaning.

  If two relative dates are specified, the first is counted backward from
  "today", and the second is counted forward from that.

  >>> import datetime
  >>> today = datetime.datetime(2008, 8, 31)
  >>> dates_from_path('/2008-08-04', '/2008-08-11')
  (datetime.date(2008, 8, 4), datetime.date(2008, 8, 11))

  >>> dates_from_path('4w', None, today)
  (datetime.date(2008, 8, 3), datetime.date(2008, 8, 31))

  >>> dates_from_path('4m', '2m', today)
  (datetime.date(2008, 4, 30), datetime.date(2008, 6, 30))
  """
  if not spath:
    spath = default_start
  if not epath:
    epath = 'today'
  if today is None:
    today = datetime.date.today()

  # Handle datetime objects (we like date objects better)
  if hasattr(today, 'date') and hasattr(today.date, "__call__"):
    today = today.date()

  spath = spath.strip('/')
  epath = epath.strip('/')

  # parses the date or returns None if unsuccessful
  if spath == 'today':
    sdate = today
  else:
    sdate = try_to_parse_absolute_date(spath)
    if not sdate:
      # Not an absolute date: try a relative one
      delta = DateDelta.fromstring(spath)
      delta.invert()  # reverse the sense: going backward
      sdate = delta.add_to_date(today)

  if epath == 'today':
    edate = today
  else:
    edate = try_to_parse_absolute_date(epath)
    if not edate:
      # Try relative to the sdate
      delta = DateDelta.fromstring(epath)
      edate = delta.add_to_date(sdate)

  return sdate, edate

if __name__ == "__main__":
  import doctest
  doctest.testmod()
