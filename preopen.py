#!/usr/bin/env python3
"""Pre-US-open brief (Q asked 2026-11-21): which way Asia, Europe and US futures are pointing.

GitHub Actions preopen.yml. Scheduled runs start hours late, so several cron times fire;
the first run between 09:00 and 14:25 UTC (19:00 Brisbane to just before the US open) sends,
then the others see the day's sent marker (actions cache) and exit. `--test` sends now; `--dry` prints.
"""
import argparse, datetime as dt, json, sys
from pathlib import Path
from zoneinfo import ZoneInfo
from alert import send

BNE, NY = ZoneInfo("Australia/Brisbane"), ZoneInfo("America/New_York")
STATE = Path("state_preopen/sent.json")
US_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18"}  # NY dates

ASIA = [("^TWII", "Taiwan"), ("^N225", "Japan"), ("^KS11", "Korea"), ("^HSI", "Hong Kong"),
        ("000001.SS", "China"), ("^AXJO", "Australia")]
EUROPE = [("^GDAXI", "Germany"), ("^STOXX50E", "Euro Stoxx 50"), ("^FTSE", "UK")]
FUT = [("NQ=F", "Nasdaq futures"), ("ES=F", "S&P futures")]
OTHER = [("GC=F", "Gold"), ("JPY=X", "USD/JPY"), ("BTC-USD", "Bitcoin")]


def change(tk):
    """(last, % change vs previous close, date of last bar) or None."""
    import yfinance as yf
    t = yf.Ticker(tk)
    try:
        fi = t.fast_info
        last, prev = float(fi["last_price"]), float(fi["previous_close"])
        h = t.history(period="5d", interval="1d")["Close"].dropna()
        when = h.index[-1].date() if len(h) else None
        if last and prev:
            return last, 100 * (last / prev - 1), when
    except Exception:
        pass
    try:
        h = t.history(period="7d", interval="1d")["Close"].dropna()
        return float(h.iloc[-1]), 100 * (h.iloc[-1] / h.iloc[-2] - 1), h.index[-1].date()
    except Exception:
        return None


def line(name, r, today):
    if not r:
        return f"{name}: no data"
    last, pct, when = r
    stale = "" if when is None or when >= today - dt.timedelta(days=1) else f" (data from {when:%a %d %b})"
    return f"{name}: {pct:+.1f}%{stale}"


def verdict(asia, fut):
    a = [r[1] for r in asia if r]
    nq = fut[0][1] if fut[0] else None
    avg = sum(a) / len(a) if a else 0.0
    if nq is None:
        return "Unclear (no futures data)."
    if nq <= -1.0 or avg <= -1.5:
        return "Pointing DOWN."
    if nq >= 0.7 and avg >= 0:
        return "Pointing UP."
    return "Mixed / no clear direction."


def build():
    import yfinance as yf
    today = dt.datetime.now(BNE).date()
    asia = [change(t) for t, _ in ASIA]
    eur = [change(t) for t, _ in EUROPE]
    fut = [change(t) for t, _ in FUT]
    oth = [change(t) for t, _ in OTHER]
    try:
        d = yf.download(["^GSPC", "^VIX"], period="10d", progress=False, auto_adjust=True)["Close"].dropna()
        spx, vix = float(d["^GSPC"].iloc[-1]), float(d["^VIX"].iloc[-1])
    except Exception:
        spx = vix = None
    v = verdict(asia, fut)
    out = [f"<b>{v}</b>", ""]
    out += ["<b>Asia today</b>"] + [line(n, r, today) for (_, n), r in zip(ASIA, asia)] + [""]
    out += ["<b>Europe so far</b>"] + [line(n, r, today) for (_, n), r in zip(EUROPE, eur)] + [""]
    out += ["<b>US futures</b>"] + [line(n, r, today) for (_, n), r in zip(FUT, fut)] + [""]
    out += ["<b>Other</b>"] + [line(n, r, today) for (_, n), r in zip(OTHER, oth)] + [""]
    if spx and vix:
        out += [f"<b>At the US close, serious if:</b> S&P below {spx:,.0f}, VIX above 40, "
                "equal-weight (RSP) and junk bonds (HYG) falling too, banks breaking.",
                "Normal if: S&P holds, VIX back under ~33, selling stays in tech.", ""]
    out += ["A hint only. No action needed before the US open; your orders and FTD rule stay the same."]
    return v, "<br>".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    now_u = dt.datetime.now(dt.timezone.utc)
    now_ny = now_u.astimezone(NY)
    today = now_u.date().isoformat()
    if not (a.test or a.dry):
        if now_u.weekday() >= 5 or now_ny.date().isoformat() in US_HOLIDAYS:
            print("weekend/holiday"); return
        if not dt.time(9, 0) <= now_u.time() <= dt.time(14, 25):
            print(f"{now_u:%H:%M} UTC outside 09:00-14:25 window"); return
        try:
            if json.loads(STATE.read_text()).get("sent") == today:
                print("already sent today"); return
        except Exception:
            pass
    v, html = build()
    subj = ("TEST: " if a.test else "") + f"Before US open: {v.rstrip('.')}"
    if a.dry:
        print(subj); print(html.replace("<br>", "\n")); return
    send(subj, html)
    if not a.test:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"sent": today}))


if __name__ == "__main__":
    main()
