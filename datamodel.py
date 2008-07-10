from __future__ import division

import datetime
import logging

from google.appengine.ext import db

BLOCK_SIZE=35  # never change this!
DEFAULT_QUERY_SIZE=35
DEFAULT_QUERY_DAYS=14

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

    logging.info("start_day %s" % start_day)
    logging.info("end_day %s" % end_day)

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
      logging.info("block %d" % block.day_zero)
      for rel_day, weight in enumerate(block.weight_entries):
        day = rel_day + block.day_zero
        if start_day <= day <= end_day and weight >= 0.0:
          yield datetime.date.fromordinal(day), weight

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
