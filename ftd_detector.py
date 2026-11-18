"""IBD-style follow-through-day (FTD) detector for the daily re-entry alert (Q 2026-11-19: FTD = add more, never the first buy).
Checked 2018-2026 on ^IXIC / ^GSPC: finds IBD's FTDs of 2 Apr 2020, 18 Mar / 24 Jun / 21 Oct 2022, 22 Apr 2025.
"""
import pandas as pd, numpy as np
def ftd_days(df, min_dd=0.07, gain=0.012, min_day=4, max_day=25):
    """IBD-style follow-through days.  df: close, volume (daily).  A rally attempt starts at a closing low made while the index is
    >= min_dd below its 52-week closing high; day 1 = first higher close after that low; a close below the low resets the attempt.
    FTD = on attempt day min_day..max_day, close up >= gain on volume above the prior day's.  One FTD per attempt."""
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float); idx = df.index
    hi = pd.Series(c).rolling(252, min_periods=20).max().to_numpy()
    out = []; low_i = None; day = 0; done = False; active = None
    for i in range(1, len(c)):
        if low_i is not None and done and c[i] >= hi[i] * 0.97: low_i = None; active = None            # recovered to near the high: the next correction starts a fresh attempt
        if active is not None and c[i] < active['low']:
            out.append(dict(kind='failed', date=idx[i].date(), ftd_date=active['date'], low=active['low'], close=c[i])); active = None
        if low_i is None or c[i] < c[low_i]:
            if c[i] <= hi[i] * (1 - min_dd): low_i, day, done = i, 0, False      # new correction low: (re)start
            elif low_i is not None and c[i] < c[low_i]: low_i = None
            continue
        if day == 0:
            if c[i] > c[i - 1]: day = 1
            continue
        day += 1
        if not done and min_day <= day <= max_day and c[i] / c[i - 1] - 1 >= gain and v[i] > v[i - 1] and v[i] > 0:
            out.append(dict(kind='ftd', date=idx[i].date(), low_date=idx[low_i].date(), low=c[low_i], close=c[i], day=day, gain=c[i] / c[i - 1] - 1,
                            off_low=c[i] / c[low_i] - 1, dd_at_low=c[low_i] / hi[low_i] - 1)); done = True; active = out[-1]
        if day > max_day and not done: low_i = None                                # attempt expired without an FTD
    return out
