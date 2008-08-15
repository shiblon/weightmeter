from __future__ import division
import datetime
import logging
import math

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

def chartserver_data_params(entries, width, height):
  """Create chartserver url data parameters

  If you want to limit the number of samples received, you must sample *before*
  calling this function.  Also note that the chart is called as though there
  are no missing day values, so it will plot all points equally spaced no
  matter what the dates say.

  Note that the image size will be adjusted to retain the same aspect ratio,
  but be of smaller total size if it contains more than 300,000 pixels (per
  charts api maximum).

  Args:
    entries: iterable over rows, first column date, remaining are values to be
        plotted.  The number of lines on the plot will be equal to columns - 1
    width: actual chartserver image width in pixels
    height: actual chartserver image height in pixels

  Returns:
    A list of chartserver url parameters (what will be separated by & in the
    final URL)

  """
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

def chartserver_bounded_size(width, height):
  # bound the width and height so that the product does not exceed 300,000.
  # Also, the maximum individual component is 1000.
  if width > 1000:
    height = height * 1000 // width
    width = 1000
  if height > 1000:
    width = width * 1000 // height
    height = 1000
  prod = width * height
  if prod > 300000:
    ratio = math.sqrt(300000 / prod)
    width = int(width * ratio)
    height = int(height * ratio)
  return width, height

def chartserver_weight_url(width, height, smoothed_iter):
  logging.debug("w=%d h=%d" % (width, height))
  params = [
      "chs=%dx%d" % (width, height),
      "cht=lc",
      "chm=D,ccddff,1,0,6|D,4488ff,0,0,2|d,4488ff,0,-1,6",
      ]
  params.extend(
      chartserver_data_params(
        smoothed_iter,
        width=width,
        height=height)
      )
  return "http://chart.apis.google.com/chart?" + "&".join(params)
