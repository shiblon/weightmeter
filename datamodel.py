from __future__ import division

import datetime
import logging

from google.appengine.ext import db
from itertools import izip

BLOCK_SIZE=35  # never change this!
DEFAULT_QUERY_SIZE=35
DEFAULT_QUERY_DAYS=14
DECAY_SETUP_DAYS = 14

class UserInfo(db.Expando):
  user = db.UserProperty(required=True)
  scale_resolution = db.FloatProperty(required=True, default=0.5)
  gamma = db.FloatProperty(required=True, default=0.9)

class WeightBlock(db.Model):
  """Contains a block of weight entries, starting with day_zero (in Proleptic
  Gregorian ordinal days (Jan 1 of AD 1 = 1: you can get this by calling
  date.toordinal()) and containing 35 days (5 weeks)
  of weight entries.
  """
  user_info = db.ReferenceProperty(UserInfo, required=True)
  day_zero = db.IntegerProperty()  # in days since the Epoch
  weight_entries = db.ListProperty(float)

  @staticmethod
  def _WeightBlock_key(user_info, day_zero):
    return db.Key.from_path(
        'WeightBlock',
        WeightBlock._WeightBlock_key_name(day_zero),
        parent=user_info)

  @staticmethod
  def _WeightBlock_key_name(day_zero):
    return "d:%07d" % day_zero

class WeightData(object):
  """An abstraction to allow for easy, on-demand access to daily entries,
  supporting the somewhat weird underlying data model that we have to use to
  keep things sane in retrieval of large date ranges.
  """
  def __init__(self, user_info):
    """Create a WeightData object for the given user

    Args:
      user_info: required UserInfo object, obtained from the datastore.
    """
    self.user_info = user_info

  @staticmethod
  def _day_zero(day):
    return day - (day % BLOCK_SIZE)

  def _get_block(self, day_zero):
    return WeightBlock.get_or_insert(
        key_name=WeightBlock._WeightBlock_key_name(day_zero),
        parent=self.user_info,
        user_info=self.user_info,
        weight_entries=[-1.0] * BLOCK_SIZE,
        day_zero=day_zero)

  def most_recent_entry(self):
    """Queries the database for the most recent weight entry that it can find.

    If there isn't one, it returns None, otherwise it returns a date,weight
    pair.
    """
    end_day = datetime.date.today().toordinal()
    day_zero = self._day_zero(end_day)

    query = WeightBlock.gql(
        "WHERE user_info = :1 AND "
        "day_zero <= :2 "
        "ORDER BY day_zero DESC",
        self.user_info, day_zero)

    values = query.fetch(1)
    if not values:
      return None
    else:
      block = values[0]
      for rel_day in range(BLOCK_SIZE-1, -1, -1):
        entry = block.weight_entries[rel_day]
        if entry >= 0.0:
          return datetime.date.fromordinal(block.day_zero + rel_day), entry
      else:
        # Nothing found in the block, it might as well not be there
        return None

  def query(self, start_date=None, end_date=None):
    """Query the datastore for weight values.

    If start_date is not specified, it defaults to DEFAULT_QUERY_DAYS before
    end_date.  If end_date is not specified, it defaults to "today".  If
    max_values is not specified, it defaults to DEFAULT_QUERY_SIZE.

    Args:
      start_date: first date of data that interests us.  Default end_date -
          DEFAULT_QUERY_DAYS
      end_date: last date of interesting data.  Default today.

    Returns:
      <date, weight> pair iterator
    """
    if end_date is None:
      end_date = datetime.date.today()

    if start_date is None:
      start_date = end_date - datetime.timedelta(days=DEFAULT_QUERY_DAYS)

    start_day = start_date.toordinal()
    end_day = end_date.toordinal()

    assert start_day < end_day

    num_days = end_day - start_day

    logging.debug("start_day %s" % start_day)
    logging.debug("end_day %s" % end_day)

    start_day_zero = self._day_zero(start_day)
    end_day_zero = self._day_zero(end_day)

    # Now we run the query:
    query = WeightBlock.gql(
        "WHERE user_info = :1 AND "
        "day_zero >= :2 AND day_zero <= :3 "
        "ORDER BY day_zero ASC",
        self.user_info, start_day_zero, end_day_zero)

    # Iterate over all of the non-empty dates from start_day to end_day within
    # the blocks:
    for block in query:
      logging.debug("block %d" % block.day_zero)
      for rel_day, weight in enumerate(block.weight_entries):
        day = rel_day + block.day_zero
        if start_day <= day <= end_day and weight >= 0.0:
          yield datetime.date.fromordinal(day), weight

  def smoothed_weight_iter(self, start, end, samples, gamma=0.9):
    # Start a few days early so that we can get the smoothing primed
    early_d1 = start - datetime.timedelta(days=DECAY_SETUP_DAYS)
    early_d2 = start
    early_smoothed = list(
        decaying_average_iter(full_entry_iter(self.query(early_d1, early_d2))))
    smooth_start = None
    if early_smoothed:
      smooth_start = early_smoothed[-1][-1]

    # Get the sampled raw weights and smoothed function:
    smoothed_iter = decaying_average_iter(
        sample_entries(self.query(start, end), start, end, samples),
        gamma=gamma,
        start=smooth_start)

    return smoothed_iter

  def update(self, date, weight):
    """Update the weight for a given date
    
    Args:
      date: the day to update
      weight: the weight to update
    """
    day = date.toordinal()
    day_zero = self._day_zero(day)

    block = self._get_block(day_zero)

    block_index = day - day_zero
    assert 0 <= block_index < BLOCK_SIZE

    block.weight_entries[block_index] = weight
    block.put()

  def batch_update(self, entries):
    """Update a batch of weights.

    This is much more efficient than just doing one at a time because it splits
    things up into blocks and only updates each block once.

    Args:
      entries: a list (not just an iterable) of date,weight pairs
    """
    assert len(entries) > 0
    entries.sort()  # sort by date

    # Fill up blocks.  Note that we don't have to make any queries if we have
    # data enough to completely fill a block, since that means the whole block
    # will be overwritten.  TODO: take advantage of this fact.
    #
    # TODO: If we have a block full of nothing, delete it altogether.

    block = None
    for date, weight in entries:
      day = date.toordinal()
      day_zero = self._day_zero(day)
      # If we don't have this block yet, get_or_insert it
      if block is None or day_zero != block.day_zero:
        # When we see a new block, commit the last one and then overwrite it.
        if block is not None:
          block.put()
        # Get the new block for the current date
        block = self._get_block(day_zero)
      block.weight_entries[day - day_zero] = weight
    else:
      # We've got one hanging out there that needs to be committed.
      # Note that because we assert that we have at least one entry, we will
      # always have a final block to put, so there is no need to test for None.
      block.put()

def full_entry_iter(entries):
  """Take entries from the datastore, which may have gaps, and return an
  iterator that fills in those gaps with "None" entries.

  Args:
    entries: list of date,weight pairs
  """
  entry_iter = entries
  try:
    date, weight = entry_iter.next()
    yield date, weight
    day = date.toordinal()
    for date, weight in entries:
      last_day = day
      day = date.toordinal()
      for i in xrange(last_day + 1, day):
        # Fill in the intervening space with "None" entries
        yield datetime.date.fromordinal(i), None
      yield date, weight
  except StopIteration, e:
    # Do nothing - no entries means nothing to do!
    pass

def scan_convert_line(x1, y1, x2, y2):
  """Yields coordinates that are "on" for a scan converted line, according to
  the midpoint algorithm.

  This is useful for the kind of sampling that we want to do: it is
  essentially linear in nature, since we're just defining a slope that is
  different from 1 as we choose to sample the space of weight entries.

  For sampling, one would set the x axis up as the full complement of data,
  and the y axis as the amount of data that is actually desired.  That
  ensures that the slope is between 0 and 1.
  """
  assert x2 >= x1
  assert (x2 - x1) >= (y2 - y1)

  dx = x2 - x1
  dy = y2 - y1
  d = 2 * dy - dx
  y = y1
  for x in xrange(x1, x2 + 1):
    yield x, y
    if d > 0:
      d += 2 * (dy - dx)
      y += 1
    else:
      d += 2 * dy

def sample_entries(entry_iter, start_date, end_date, num_samples):
  """Sample weight information from a set of dates.

  Used to downsample weight data so that it can be reasonably graphed, e.g.,
  by the Google Chart API.

  Args:
    entry_iter: iterator over date,weight pairs
    start_date: first date to sample
    end_date: last date to sample
    num_samples: number of values to obtain
  """
  # Make sure we have all dates in the range, missing entries represented as
  # None.
  entry_iter = full_entry_iter(entry_iter)

  weight = None
  # Advance the iterator until we're at the start date

  date = None
  for date, weight in entry_iter:
    if date >= start_date:
      break

  if date is None:
    return

  logging.debug("real start date: %r" % date)
  start_date = date

  num_dates = end_date.toordinal() - start_date.toordinal() + 1

  logging.debug("num dates: %d" % num_dates)

  # If there is no need to sample, don't bother, just yield the values.
  if num_samples >= num_dates:
    # We have already consumed the start date, so we yield it first
    yield date, weight
    # Now consume as many as we can
    for i, (date, weight) in enumerate(entry_iter):
      if i >= num_samples or date > end_date:
        break
      else:
        yield date, weight
    return

  # Now start the line scan conversion algorithm to get the actual samples.
  # Everything up to and including a sample is averaged with it to produce
  # the value that is yield-ed.

  # We already consumed one member of the iterator, so we use it here
  if weight is not None:
    weight_sum = weight
    num_weights = 1
  else:
    weight_sum = 0.0
    num_weights = 0

  # Now we run the midpoint algorithm.  Every time 'y' changes, we emit a
  # sample up to but not including that 'x', where 'x' is 'entry' and 'y' is
  # 'sample'.  In other words, we put the raw data along the x axis, and the
  # sampled data along the y axis.  We accumulate averages as long as y is
  # unchanging.

  use_samples = min(num_samples, num_dates)

  logging.debug("use samples: %d" % use_samples)

  line_iter = scan_convert_line(
      start_date.toordinal(), 0, end_date.toordinal(), use_samples - 1)
  dummy = line_iter.next()  # always returns start_date.toordinal(), 0
  last_sample_index = 0
  for (date, weight), (day, sample_index) in izip(entry_iter, line_iter):
    assert date.toordinal() == day
    if sample_index != last_sample_index:
      # Emit the accumulated value
      sample_date = datetime.date.fromordinal(day - 1)
      if num_weights == 0:  # no non-None values found this round
        yield sample_date, None
      else:
        yield sample_date, weight_sum / num_weights

      # Start the cycle over
      num_weights = 0
      weight_sum = 0.0

    if weight is not None:
      weight_sum += weight
      num_weights += 1

    last_sample_index = sample_index
  else:
    # One final emission at the very end
    if num_weights == 0:
      yield end_date, None
    else:
      yield end_date, weight_sum / num_weights

def decaying_average_iter(
    entry_iter, start=None, gamma=None, propagate_missing=False):
  """Produce *entry, running_average data for each entry

  Produces an exponentially weighted decaying average for every entry.

  Args:
    entry_iter: an iterator over date,weight pairs
    start: start value for decayed average (will be the first value)
    gamma: the multiplier to use for exponentially weighted smoothing (0.9)
    propagate_missing (False): if True, a None value in the data produces a
        None value in the average.  Otherwise, it simply leaves the average
        unchanged.  Note that *initial* None values will *still* produce None
        in the output even if this is False.

  Returns:
    An iterator over *entry,running_average values
  """
  if gamma is None:
    gamma = 0.9

  smoothed = start
  for date, weight in entry_iter:
    if smoothed is None:
      smoothed = weight

    if weight is None:
      if propagate_missing:
        yield date, weight, None
      else:
        yield date, weight, smoothed  # retain previous smoothed value
    else:
      # weight entry exists - calculate the new smoothed value
      smoothed = gamma * smoothed + (1 - gamma) * weight
      yield date, weight, smoothed

