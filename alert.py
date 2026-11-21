#!/usr/bin/env python3
"""Daily market alert email (runs on GitHub Actions after each US close).

Checks VIX, index levels, S&P 500 trend/breadth and IBD-style follow-through days, and emails once when a
condition first becomes true (it re-arms once the condition is false again). The levels are not in this file:
they come from the ALERT_CONFIG secret (JSON), so the repo can be public. No orders, no broker connection.

ALERT_CONFIG example:
  {"vix": [30, 35, 45], "down": {"spx": [7000], "ndx": [25000]}, "up": {"ndx": [30000]},
   "backstop": {"QQQ": 661.71}, "crash": {"spx_dd": 0.20, "vix": 35, "tech_gap": 0.12}, "note": "free text shown in the email"}
"backstop": fund -> sale price; emails when the fund CLOSES at or above it (buy back the unfilled rest).
Env: SMTP_USER, SMTP_PASS, ALERT_TO, ALERT_CONFIG, STATE_FILE (default state/state.json).  --test sends a sample email.
"""
import io, json, os, smtplib, sys, urllib.request, warnings
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import yfinance as yf
from ftd_detector import ftd_days

warnings.filterwarnings("ignore")
STATE = Path(os.environ.get("STATE_FILE", "state/state.json"))
NAMES = {"spx": "S&P 500", "ndx": "Nasdaq-100", "vix": "VIX"}
TICK = {"spx": "^GSPC", "ndx": "^NDX", "vix": "^VIX"}
BEAR = {"bear_below200": "S&P 500 closed below its 200-day average for 15 trading days in a row",
        "bear_deathcross": "S&P 500 50-day average crossed below its 200-day average (death cross)",
        "bear_breadth": "fewer than 30% of S&P 500 stocks are above their 200-day average",
        "bear_minus20": "S&P 500 closed 20% or more below its 52-week high: a bear market by definition"}


def daily(tk, period="2y"):
    d = yf.download(tk, period=period, progress=False, auto_adjust=True)
    if d is None or d.empty: raise RuntimeError(f"no data for {tk}")
    return d


def sp500_breadth():
    req = urllib.request.Request("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                                 headers={"User-Agent": "market-alerts/1.0 (personal research script)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        syms = pd.read_html(io.StringIO(r.read().decode("utf-8")))[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    px = yf.download(syms, period="1y", progress=False, auto_adjust=True, threads=True)["Close"]
    px = px.dropna(axis=1, thresh=200)
    if px.shape[1] < 400: raise RuntimeError(f"breadth: only {px.shape[1]} stocks with data")
    last = px.ffill().iloc[-1]; m200 = px.tail(200).mean()
    return float((last > m200).mean()), px.shape[1]


def plan_table(cfg, which):
    """Optional plan rows from cfg["plan"][which]: [[stage, share, split], ...] rendered as an HTML table."""
    rows = cfg.get("plan", {}).get(which)
    if not rows: return ""
    tr = "".join("<tr>" + "".join(f"<td style='padding:2px 8px;border:1px solid #ccc'>{c}</td>" for c in r) + "</tr>" for r in rows)
    return ("<br><table style='border-collapse:collapse;margin-top:6px'><tr><th style='padding:2px 8px;border:1px solid #ccc'>Stage</th>"
            "<th style='padding:2px 8px;border:1px solid #ccc'>Share of cash</th><th style='padding:2px 8px;border:1px solid #ccc'>Nasdaq / S&amp;P</th></tr>" + tr + "</table>")


def check(cfg):
    lines, hits, notes = {}, {}, {}
    closes = {}
    for k, tk in TICK.items():
        s = daily(tk, "10d")["Close"].squeeze().dropna(); closes[k] = (float(s.iloc[-1]), str(s.index[-1].date()))
    vix, vd = closes["vix"]
    vl = sorted(cfg.get("vix", []))
    for l in vl:
        hits[f"vix{l}"] = vix > l
    top = max([l for l in vl if vix > l], default=None)
    if top is not None:
        stage = [l for l in vl if vix > l and l != vl[0]] if len(vl) > 1 else [top]
        msg = (f"<b>VIX closed at {vix:.2f}</b> on {vd}: above " + ", ".join(str(l) for l in vl if vix > l) + ".")
        notes_v = cfg.get("vix_note", {})                     # e.g. {"35": "stage 1: 15% of cash"}
        extra = "; ".join(f"VIX {l}: {notes_v[str(l)]}" for l in vl if vix > l and str(l) in notes_v)
        if extra: msg += f" Your plan: {extra}."
        for l in vl: lines[f"vix{l}"] = msg
    for ix, levels in cfg.get("down", {}).items():
        for l in levels:
            k = f"{ix}_le_{l}"; v, d = closes[ix]; hits[k] = v <= l
            lines[k] = f"<b>{NAMES[ix]} closed at {v:,.0f}</b> on {d}, at or below your level {l:,}."
    for ix, levels in cfg.get("up", {}).items():
        for l in levels:
            k = f"{ix}_ge_{l}"; v, d = closes[ix]; hits[k] = v >= l
            lines[k] = f"<b>{NAMES[ix]} closed at {v:,.0f}</b> on {d}, above your level {l:,}."
    for tk, sale in cfg.get("backstop", {}).items():
        try:
            b = daily(tk, "10d")["Close"].squeeze().dropna(); v, d = float(b.iloc[-1]), str(b.index[-1].date())
        except Exception as e:
            print(f"backstop {tk} skipped: {e}"); continue
        # value: a price, or {"price": p, "closes": n, "since": "YYYY-MM-DD"} = n closes in a row at/above p, counting only closes on/after `since` (the sale date)
        spec = sale if isinstance(sale, dict) else {"price": sale}
        price, n = float(spec["price"]), int(spec.get("closes", 1))
        after = b[b.index >= pd.Timestamp(spec["since"])] if spec.get("since") else b
        run = after.tail(n)
        k = f"backstop_{tk}"; hits[k] = bool(len(run) == n and (run >= price).all())
        lines[k] = (f"<b>Backstop: {tk} closed at {v:,.2f}</b> on {d}" + (f" ({n} closes in a row)" if n > 1 else "") +
                    f" at or above your sale price {price:,.2f}. Your rule: buy back whatever of {tk} is still unfilled.")
    # currency vs its simple moving averages (cfg "fx_ma": {"AUDUSD=X": {"20": "plan text", "50": "plan text"}}):
    # emails when the close goes ABOVE the n-day simple average; re-arms when it closes back below.
    for tk, spec in cfg.get("fx_ma", {}).items():
        nm = tk.replace("=X", "")[:3] + "/" + tk.replace("=X", "")[3:6]
        try:
            s = daily(tk, "1y")["Close"].squeeze().dropna(); v, d = float(s.iloc[-1]), str(s.index[-1].date())
        except Exception as e:
            print(f"fx {tk} skipped: {e}"); continue
        for n, note in (spec.items() if isinstance(spec, dict) else ((str(x), "") for x in spec)):
            m = float(s.rolling(int(n)).mean().iloc[-1]); k = f"fx_{tk}_ma{n}"; hits[k] = v > m
            lines[k] = (f"<b>{nm} closed at {v:.4f}</b> on {d}, above its {n}-day simple average ({m:.4f})." + (f" Your plan: {note}." if note else ""))
            print(f"{nm} {v:.4f} vs {n}-day {m:.4f}")
    # S&P 500 trend + breadth
    sp = daily("^GSPC")["Close"].squeeze().dropna(); m50, m200 = sp.rolling(50).mean(), sp.rolling(200).mean()
    below = (sp < m200).tail(15)
    hits["bear_below200"] = bool(len(below) == 15 and below.all())
    hits["bear_deathcross"] = bool(m50.iloc[-1] < m200.iloc[-1])
    hits["bear_minus20"] = bool(sp.iloc[-1] <= sp.tail(252).max() * 0.80)
    notes.update(spx=float(sp.iloc[-1]), m50=float(m50.iloc[-1]), m200=float(m200.iloc[-1]), hi=float(sp.tail(252).max()))
    try:
        b, n = sp500_breadth(); hits["bear_breadth"] = b < 0.30; notes["breadth"] = b
    except Exception as e: print(f"breadth skipped: {e}")
    ctx = (f"S&amp;P 500 {notes['spx']:,.0f}; 50-day {notes['m50']:,.0f}; 200-day {notes['m200']:,.0f}; 52-week high {notes['hi']:,.0f}"
           + (f"; {notes['breadth']:.0%} of S&amp;P stocks above their 200-day" if "breadth" in notes else ""))
    for k, t in BEAR.items(): lines[k] = f"<b>Bull-market-end signal:</b> {t}. ({ctx})"
    # crash scenarios (cfg "crash": {"spx_dd": 0.20, "vix": 35, "tech_gap": 0.12})
    cr = cfg.get("crash")
    if cr:
        nd = daily("^NDX")["Close"].squeeze().dropna()
        spdd = float(sp.iloc[-1] / sp.tail(252).max() - 1); nddd = float(nd.iloc[-1] / nd.tail(252).max() - 1)
        crash = spdd <= -cr.get("spx_dd", 0.20) and vix > cr.get("vix", 35)
        techled = crash and (nddd - spdd) <= -cr.get("tech_gap", 0.12)
        hits["scen_crash"] = bool(crash and not techled); hits["scen_crash_tech"] = bool(techled)
        st_ = f"S&amp;P 500 {spdd:+.0%} from its high, Nasdaq-100 {nddd:+.0%}, VIX {vix:.1f}"
        lines["scen_crash"] = (f"<b>Crash scenario B is on</b> ({st_}). Your plan: buy at your stages mainly Nasdaq/tech (QQQ, the index, not single AI stocks), plus some S&amp;P; add more tech after a follow-through day." + plan_table(cfg, "crash"))
        lines["scen_crash_tech"] = (f"<b>Tech-led crash</b> ({st_}: the Nasdaq is falling much harder than the S&amp;P). Your plan: buy the S&amp;P first at your stages; add tech only after a follow-through day." + plan_table(cfg, "crash_tech"))
    # follow-through days (event keys, last 3 sessions)
    events = {}
    for tk, nm in (("^IXIC", "Nasdaq Composite"), ("^GSPC", "S&P 500")):
        d = daily(tk)[["Close", "Volume"]].dropna(); d.columns = ["close", "volume"]; recent = set(d.index[-3:].date)
        for e in ftd_days(d):
            if e["date"] not in recent: continue
            if e["kind"] == "ftd":
                events[f"ftd_{tk[1:]}_{e['date']}"] = (f"<b>{nm} follow-through day</b> on {e['date']}: +{e['gain']:.1%} on higher volume, day {e['day']} "
                                                       f"of the rally from the {e['low_date']} low ({e['off_low']:+.1%} off that low)." + (f" Your plan: {cfg['ftd_note']}." if cfg.get("ftd_note") else ""))
            else:
                events[f"ftdfail_{tk[1:]}_{e['date']}"] = (f"<b>{nm} follow-through day of {e['ftd_date']} FAILED</b> on {e['date']}: "
                                                          f"closed at {e['close']:,.0f}, below the rally low {e['low']:,.0f}.")
    # cfg "skip": ["spx_le_7115", ...] drops an alert without re-entering the whole ALERT_CONFIG (level compared as a number)
    def _same(a, b):
        (pa, la), (pb, lb) = a.rsplit("_", 1), b.rsplit("_", 1)
        try: return pa == pb and float(la) == float(lb)
        except ValueError: return a == b
    for s in cfg.get("skip", []):
        for k in [k for k in hits if k == s or ("_" in k and _same(k, s))]: hits.pop(k); print(f"skipped {k}")
    summary = f"VIX {vix:.2f}, S&amp;P 500 {closes['spx'][0]:,.0f}, Nasdaq-100 {closes['ndx'][0]:,.0f} (closes of {closes['spx'][1]})."
    return hits, lines, events, summary


# SEC filing watch (Q 2026-11-21, NVDA accounting review reported 2026-11-20). cfg "sec_watch" overrides {"TICKER": CIK}.
# Event = in the last 10 days: an 8-K with Item 4.01 (auditor change) or 4.02 (restatement / non-reliance), a late-filing notice
# (NT 10-Q / NT 10-K), or a 10-Q/10-K whose text mentions an SEC investigation, subpoena or Wells notice (snippet quoted).
SEC_WATCH = {"NVDA": 1045810}


def sec_events(cfg):
    import re, urllib.request, datetime as dt
    ua = {"User-Agent": f"market-alerts personal script {os.environ.get('SMTP_USER', '')}".strip(), "Accept-Encoding": "identity"}
    def get(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30) as r: return r.read().decode("utf-8", "replace")
    ev, since = {}, (dt.date.today() - dt.timedelta(days=10)).isoformat()
    for tk, cik in (cfg.get("sec_watch") or SEC_WATCH).items():
        try:
            rec = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))["filings"]["recent"]
        except Exception as e:
            print(f"sec {tk} skipped: {e}"); continue
        for form, date, acc, items, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec.get("items", [""] * len(rec["form"])), rec["primaryDocument"]):
            if date < since: break
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
            key, why = f"sec_{tk}_{acc}", None
            if form.startswith("8-K") and any(i in (items or "") for i in ("4.01", "4.02")):
                why = ("restatement" if "4.02" in items else "auditor changed")
            elif form.startswith("NT 10"):
                why = "late filing"
            elif form in ("10-Q", "10-K"):
                try:
                    txt = re.sub(r"<[^>]+>|&#?\w+;", " ", get(url)); txt = re.sub(r"\s+", " ", txt)
                    m = re.search(r"[^.]{0,250}(?:SEC|Securities and Exchange Commission)[^.]{0,250}(?:investigat|subpoena|Wells notice)[^.]{0,200}\.|"
                                  r"[^.]{0,250}(?:investigat|subpoena|Wells notice)[^.]{0,250}(?:SEC|Securities and Exchange Commission)[^.]{0,200}\.", txt)
                    if m: why = f"SEC investigation mentioned: \"{m.group(0).strip()[:250]}\""
                except Exception as e: print(f"sec {tk} {form} text skipped: {e}")
            if why:
                ev[key] = (f"<b>{tk}: {why}</b> ({date}). <a href='{url}'>Filing</a>.<br>"
                           f"Do: cancel the {tk} order, sell any {tk}, pause AVGO + KLAC. Nothing else changes.")
    return ev


def send(subject, html):
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = [x.strip() for x in (os.environ.get("ALERT_TO") or user).split(",") if x.strip()]
    msg = MIMEText(html, "html", "utf-8"); msg["Subject"] = subject; msg["From"] = f"Market Alerts <{user}>"; msg["To"] = ", ".join(to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw); s.sendmail(user, to, msg.as_string())
    print("sent:", subject)


def main():
    test = "--test" in sys.argv
    cfg = json.loads(os.environ.get("ALERT_CONFIG") or "{}")
    cfg.update(json.loads(os.environ.get("FX_CONFIG") or "{}"))           # FX_CONFIG secret: extra settings (fx_ma, skip) merged over ALERT_CONFIG, so that secret need not be re-entered
    hits, lines, events, summary = check(cfg)
    events.update(sec_events(cfg))
    print(summary, "| hit:", [k for k, v in hits.items() if v] or "none", "| events:", list(events) or "none")
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    alerted, seen = st.get("alerted", {}), set(st.get("events_sent", []))
    new = [k for k in hits if hits[k] and not alerted.get(k)]
    new_ev = [k for k in events if k not in seen]
    if test or new or new_ev:
        body = []
        for k in new:
            if lines.get(k) and lines[k] not in body: body.append(lines[k])
        body += [events[k] for k in new_ev]
        if test: body = ["Test email: the daily check ran.", *body]
        if any(k.startswith("sec_") for k in new_ev): subj = "NVDA SEC alert"
        elif new_ev and all(k.startswith("ftd_") for k in new_ev) and not new: subj = "Follow-through day"
        elif new_ev and not new: subj = "Follow-through day FAILED"
        elif new and all(k.startswith("backstop_") for k in new): subj = "Backstop: back at your sale price"
        elif new and all(k.startswith("fx_") for k in new): subj = "Currency: back above its average"
        elif "scen_crash_tech" in new: subj = "Tech-led crash: S&P first"
        elif "scen_crash" in new: subj = "Crash scenario: buy tech at your stages"
        elif new and all(k in BEAR for k in new): subj = "Bull-market-end signal"
        elif new == [f"vix{min(cfg.get('vix', [0]))}"]: subj = "Early warning: VIX"
        else: subj = "Market alert"
        html = "<ul>" + "".join(f"<li>{b}</li>" for b in body) + f"</ul><p>Now: {summary}</p>"
        if cfg.get("note"): html += f"<p>{cfg['note']}</p>"
        html += "<p>A reminder of your own rules, not advice; nothing has been bought or sold.</p>"
        send(("TEST: " if test else "") + subj, html)
    if not test:
        st = {"alerted": {k: bool(v) for k, v in hits.items()}, "events_sent": sorted(seen | set(events))[-60:]}
        STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
