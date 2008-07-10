from __future__ import division
import datetime
import logging

from itertools import izip

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

  logging.info("real start date: %r" % date)
  start_date = date

  num_dates = end_date.toordinal() - start_date.toordinal() + 1

  logging.info("num dates: %d" % num_dates)

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

  logging.info("use samples: %d" % use_samples)

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

def decaying_average_iter(entry_iter, gamma=None, propagate_missing=False):
  """Produce *entry, running_average data for each entry

  Produces an exponentially weighted decaying average for every entry.

  Args:
    entry_iter: an iterator over date,weight pairs
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

  smoothed = None
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

def chartserver_simple_encode(values, mn=None, mx=None):
  S = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  N = len(S)

  if mn is None:
    mn = min(values)
  if mx is None:
    mx = max(values)

  rng = mx - mn
  if rng <= 0.01:
    return ''

  enc = []
  for v in values:
    if v is None:
      enc.append('_')
    else:
      enc.append(S[int((N - 1) * (v - mn) / rng)])
  return "".join(enc)

def chartserver_extended_encode(values, mn=None, mx=None):
  S = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-.'
  N = len(S) ** 2
  if len(values) <= 1:
    return ''

  if mn is None:
    mn = min(values)
  if mx is None:
    mx = max(values)

  rng = mx - mn
  if rng <= 0.01:
    return ''

  enc = []
  for v in values:
    if v is None:
      enc.append('__')
    else:
      quantized = int((N - 1) * (v - mn) / rng)
      assert 0 <= quantized < N
      lsc = quantized % len(S)
      msc = quantized // len(S)
      enc.append(S[msc])
      enc.append(S[lsc])
  return "".join(enc)

def chartserver_text_encode(values, mn=None, mx=None):
  N = 100

  if mn is None:
    mn = min(values)
  if mx is None:
    mx = max(values)

  enc = []
  rng = mx - mn
  for v in values:
    if v is None:
      enc.append('-1')
    else:
      scaled = N * (v - mn) / rng
      assert 0.0 <= scaled <= N
      enc.append("%0.1f" % scaled)
  return ",".join(enc)

def date_labels(dates):
  """Take the list of dates and make labels out of them.
  
  The idea is that some date ranges don't need as much displayed as others.  If
  the year is always the same, for example, then you can skip showing it.
  """
  dates = list(dates)

  # Basic rules:
  # - if only one year is represented, don't show it ("%b %d")
  #   - if only one month is represented, don't show it ("%d")
  # - else show the year *and* the month ("%b %Y")
  #   - if months are ever repeated consecutively (within the same year), show
  #     the day as well ("%b %d, %Y")
  format = "%Y-%m-%d"
  if len(set(d.year for d in dates)) == 1:
    if len(set(d.month for d in dates)) == 1:
      format = "%d"
    else:
      format = "%b %d"
  else:
    ym = [(d.year, d.month) for d in dates]
    for i in range(1, len(ym)):
      if ym[i] == ym[i-1]:
        # two consecutive labels with the same month and year: show the day
        format = "%b %d, %Y"
        break
    else:
      # No consecutive months found
      format = "%b %Y"

  return [d.strftime(format) for d in dates]

def chartserver_data_params(entries, width=None, height=None):
  """Create chartserver url parameters (everything after '?') from the data.

  If you want to limit the number of samples received, you must sample *before*
  calling this function.  Also note that the chart is called as though there
  are no missing day values, so it will plot all points equally spaced no
  matter what the dates say.

  Args:
    entries: iterable over rows, first column date, remaining are values to be
        plotted.  The number of lines on the plot will be equal to columns - 1
    width (300): image width
    height (200): image height

  Returns:
    A list of chartserver url parameters (what will be separated by & in the
    final URL)

  """
  if width is None:
    width = 300
  if height is None:
    height = 200

  params = []

  data = list(entries)
  if not data:
    return params

  earliest = min(x[0] for x in data)
  latest = max(x[0] for x in data)
  
  # min and max are usually pretty easy to calculate, but in this case it is a
  # little harder since we can have None values sprinkled throughout the whole
  # set of data.
  mn = None
  mx = None
  for row in data:
    non_none = [x for x in row[1:] if x is not None]
    if len(non_none) > 0:
      if mx is None:
        mx = max(non_none)
      else:
        mx = max(mx, *non_none)
      if mn is None:
        mn = min(non_none)
      else:
        mn = min(mn, *non_none)

  if mn is None or mx is None:
    return params

  if mn == mx:
    mn = mn - 0.1
    mx = mx + 0.1

  num_days = latest.toordinal() - earliest.toordinal()

  # maximum resolution of 0.05 lbs (1/20 lb) is plenty (handles 0.25 and 0.1,
  # but probably overkill)
  max_unique_values = 20 * (mx - mn) + 1
  logging.info("max unique: %d" % max_unique_values)

  data_param = 'chd='
  num_columns = len(data[0])

  if max_unique_values <= 62:
    logging.info("using simple encoding")
    # We can use simple encoding
    e = []
    for i in range(1, num_columns):
      e.append(chartserver_simple_encode([x[i] for x in data], mn=mn, mx=mx))
    data_param += "s:" + ",".join(e)
  elif max_unique_values <= 4096:
    # We can use extended encoding
    e = []
    for i in range(1, num_columns):
      e.append(chartserver_extended_encode([x[i] for x in data], mn=mn, mx=mx))
    data_param += "e:" + ",".join(e)
  else:
    # We have to use text encoding
    e = []
    for i in range(1, num_columns):
      e.append(chartserver_text_encode([x[i] for x in data], mn=mn, mx=mx))
    data_param += "t:" + "|".join(e)

  params.append(data_param)

  # Axis label types: one x, one y
  params.append("chxt=x,y")

  label_param = "chxl=1:|%.1f|%.1f|%.1f|0:|" % (mn, (mx + mn) / 2, mx)

  mid_date = datetime.date.fromordinal(
      int((latest.toordinal() + earliest.toordinal()) // 2))
  if mid_date == earliest or mid_date == latest:
    label_dates = earliest, latest
  else:
    label_dates = earliest, mid_date, latest

  label_param += '|'.join(date_labels(label_dates))

  params.append(label_param)

  return params
