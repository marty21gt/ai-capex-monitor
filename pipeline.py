#!/usr/bin/env python3
"""
AI Capex Stress Monitor - data pipeline.

One question: is AI capex overwhelming the growth it is meant to fund?
Discretionary context only. Nothing here feeds the Fragility Monitor gate.

Stages (every stage logs to docs/run_log.txt):
  1. Fundamentals  SEC EDGAR companyfacts -> quarterly series (YTD differencing)
  2. Segments      XBRL instance documents -> cloud / data-center segment revenue
  3. Markets       Yahoo Finance -> canary relative performance vs QQQ
  4. Alerts        EDGAR full-text search -> useful-life change disclosures
  5. Signals       ranked signal states + overall status
  6. Publish       completeness gate, then docs/data.json

Settled quarters are immutable once published. A later filing that reports a
different value is logged as a restatement and (by default) NOT applied.
Set "accept_restatements": true in config.json to apply them.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA_PATH = DOCS / "data.json"
LOG_PATH = DOCS / "run_log.txt"
CONFIG_PATH = ROOT / "config.json"

QUARTER_DAYS = (80, 100)   # a fiscal quarter: 13 or 14 weeks, or a calendar quarter
ANNUAL_DAYS = (350, 380)   # a fiscal year: 52 or 53 weeks, or a calendar year
RECON_TOL = 0.01           # direct vs derived quarters must agree within 1%
RESTATE_TOL = 0.005        # published vs latest filing: flag beyond 0.5%

LOG_LINES: list[str] = []


def log(level: str, msg: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}Z {level:<5} {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)


# --------------------------------------------------------------------------
# Quarter helpers
# --------------------------------------------------------------------------
def to_date(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


def qlabel(end: dt.date) -> str:
    """Calendar-quarter label for a period ending on `end`.

    Uses end - 45 days so off-calendar fiscal quarters land where most of the
    quarter actually sits: ORCL's quarter ending Aug 31 -> Q3, NVDA's quarter
    ending ~Oct 26 -> Q3, NVDA's fiscal year ending ~Jan 26 -> prior-year Q4.
    """
    ref = end - dt.timedelta(days=45)
    return f"{ref.year}Q{(ref.month - 1) // 3 + 1}"


def qoffset(label: str, n: int) -> str:
    y, q = int(label[:4]), int(label[5])
    idx = y * 4 + (q - 1) + n
    return f"{idx // 4}Q{idx % 4 + 1}"


def _close(a: float, b: float, tol: float = RECON_TOL) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1.0)


# --------------------------------------------------------------------------
# Quarterly derivation (the YTD trap lives here)
# --------------------------------------------------------------------------
def derive_quarterly(facts: list[dict], min_year: int = 2018):
    """Turn XBRL duration facts into discrete quarterly values.

    Cash-flow statements report year-to-date figures: Q1 (3m), H1 (6m), 9M, FY.
    Quarters are recovered by differencing consecutive facts that share a start
    date. Where a direct 3-month fact also exists, the two are reconciled and
    any disagreement beyond 1% is returned as an issue.

    Returns (values, methods, issues) keyed by calendar-quarter label.
    """
    best: dict[tuple[str, str], dict] = {}
    for f in facts:
        s, e = f.get("start"), f.get("end")
        if not s or not e:
            continue
        key = (str(s)[:10], str(e)[:10])
        if key not in best or str(f.get("filed", "")) > str(best[key].get("filed", "")):
            best[key] = f  # latest filing wins: picks up comparatives as restated

    periods: dict[tuple[dt.date, dt.date], float] = {}
    for (s, e), f in best.items():
        try:
            periods[(to_date(s), to_date(e))] = float(f["val"])
        except (TypeError, ValueError, KeyError):
            continue

    direct: dict[str, float] = {}
    derived: dict[str, list[float]] = defaultdict(list)
    by_start: dict[dt.date, list[tuple[dt.date, float]]] = defaultdict(list)

    for (s, e), v in periods.items():
        if QUARTER_DAYS[0] <= (e - s).days <= QUARTER_DAYS[1]:
            direct[qlabel(e)] = v
        by_start[s].append((e, v))

    for lst in by_start.values():
        lst.sort()
        for (pe, pv), (e, v) in zip(lst, lst[1:]):
            if QUARTER_DAYS[0] <= (e - pe).days <= QUARTER_DAYS[1]:
                derived[qlabel(e)].append(v - pv)

    out: dict[str, float] = {}
    methods: dict[str, str] = {}
    issues: list[dict] = []
    for lab in set(direct) | set(derived):
        cands = derived.get(lab, [])
        if lab in direct:
            out[lab], methods[lab] = direct[lab], "direct"
            for c in cands:
                if not _close(c, direct[lab]):
                    issues.append({"quarter": lab, "direct": direct[lab], "derived": c})
        else:
            out[lab], methods[lab] = cands[0], "ytd_difference"
            for c in cands[1:]:
                if not _close(c, cands[0]):
                    issues.append({"quarter": lab, "derived_a": cands[0], "derived_b": c})

    # Fallback for Q4 when no 9-month YTD fact exists: FY minus the three quarters.
    for (s, e), v in periods.items():
        if ANNUAL_DAYS[0] <= (e - s).days <= ANNUAL_DAYS[1]:
            lab = qlabel(e)
            if lab in out:
                continue
            prior = [qoffset(lab, -i) for i in (1, 2, 3)]
            if all(p in out for p in prior):
                out[lab] = v - sum(out[p] for p in prior)
                methods[lab] = "annual_minus_3q"

    keep = {k for k in out if int(k[:4]) >= min_year}
    return ({k: out[k] for k in keep}, {k: methods[k] for k in keep},
            [i for i in issues if int(i["quarter"][:4]) >= min_year])


def instant_quarterly(facts: list[dict], min_year: int = 2018) -> dict[str, float]:
    """Balance-sheet facts -> value at the last reported date within each quarter."""
    best: dict[str, dict] = {}
    for f in facts:
        if f.get("start") or not f.get("end"):
            continue
        e = str(f["end"])[:10]
        if e not in best or str(f.get("filed", "")) > str(best[e].get("filed", "")):
            best[e] = f
    out: dict[str, float] = {}
    dates: dict[str, dt.date] = {}
    for e, f in best.items():
        de = to_date(e)
        lab = qlabel(de)
        try:
            v = float(f["val"])
        except (TypeError, ValueError, KeyError):
            continue
        if lab not in dates or de > dates[lab]:
            out[lab], dates[lab] = v, de
    return {k: v for k, v in out.items() if int(k[:4]) >= min_year}


# --------------------------------------------------------------------------
# SEC client
# --------------------------------------------------------------------------
class SEC:
    """Polite EDGAR client: identified User-Agent, <=~7 req/s, retries."""

    def __init__(self, user_agent: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0

    def get(self, url: str, params: dict | None = None, allow_404: bool = False):
        err = None
        for attempt in range(4):
            wait = 0.15 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.get(url, params=params, timeout=90)
            except requests.RequestException as e:
                err = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code in (403, 429, 500, 502, 503, 504):
                err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"GET failed after retries ({err}): {url}")

    def json(self, url: str, params: dict | None = None):
        return self.get(url, params=params).json()


# --------------------------------------------------------------------------
# Stage 1: fundamentals
# --------------------------------------------------------------------------
def resolve_metric(usgaap: dict, concepts: list[str], kind: str, min_year: int):
    """Merge a metric across fallback concepts; earlier concepts win per quarter."""
    merged: dict[str, float] = {}
    used: list[str] = []
    issues: list[dict] = []
    for c in concepts:
        node = usgaap.get(c)
        if not node:
            continue
        facts = [f for f in node.get("units", {}).get("USD", [])
                 if str(f.get("form", "")).startswith(("10-Q", "10-K"))]
        if not facts:
            continue
        if kind == "duration":
            q, _methods, iss = derive_quarterly(facts, min_year)
        else:
            q, iss = instant_quarterly(facts, min_year), []
        added = 0
        for lab, v in q.items():
            if lab not in merged:
                merged[lab] = v
                added += 1
        if added:
            used.append(c)
        for i in iss:
            issues.append({**i, "concept": c})
    return merged, used, issues


def total_debt(m: dict[str, dict[str, float]]) -> dict[str, float]:
    """Gross debt = long-term debt (incl. current portion) + commercial paper + short-term borrowings.
    Excludes lease liabilities by design."""
    labs = set(m.get("debt_lt", {})) | set(m.get("debt_lt_noncurrent", {})) | set(m.get("debt_lt_current", {}))
    out = {}
    for lab in labs:
        lt = m.get("debt_lt", {}).get(lab)
        if lt is None:
            nc = m.get("debt_lt_noncurrent", {}).get(lab)
            cu = m.get("debt_lt_current", {}).get(lab)
            if nc is None and cu is None:
                continue
            lt = (nc or 0.0) + (cu or 0.0)
        out[lab] = lt + m.get("commercial_paper", {}).get(lab, 0.0) + m.get("short_term_borrowings", {}).get(lab, 0.0)
    return out


def fetch_fundamentals(sec: SEC, ticker: str, meta: dict, cfg: dict):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{meta['cik']:010d}.json"
    js = sec.json(url)
    name = str(js.get("entityName", ""))
    if meta["name_contains"].lower() not in name.lower():
        raise RuntimeError(f"CIK {meta['cik']} resolved to '{name}', expected '{meta['name_contains']}'. Fix config.json.")
    usgaap = js.get("facts", {}).get("us-gaap", {})
    series: dict[str, dict[str, float]] = {}
    used: dict[str, list[str]] = {}
    issues: list[dict] = []
    for kind in ("duration", "instant"):
        for metric, concepts in cfg["concepts"][kind].items():
            vals, u, iss = resolve_metric(usgaap, concepts, kind, cfg["min_year"])
            if vals:
                series[metric] = vals
                used[metric] = u
            for i in iss:
                issues.append({**i, "ticker": ticker, "metric": metric})
    td = total_debt(series)
    if td:
        series["total_debt"] = td
    latest = max((max(v) for k, v in series.items() if k in ("capex", "ocf", "revenue") and v), default=None)
    log("INFO", f"{ticker}: '{name}', {len(series)} metrics, latest quarter {latest}, "
                f"capex via {used.get('capex')}, D&A via {used.get('dna')}")
    for i in issues:
        log("WARN", f"{ticker}.{i['metric']} reconciliation {i['quarter']}: {i}")
    return series, used, issues


# --------------------------------------------------------------------------
# Stage 2: segments (cloud / data-center revenue from XBRL instance documents)
# --------------------------------------------------------------------------
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"


def parse_instance(xml_text: str, concepts: list[str], members: list[str],
                   benign: list[str], discovery_terms: list[str], filed: str):
    """Extract segment-dimensioned revenue facts from one XBRL instance.

    A fact qualifies when its context carries exactly one configured member,
    plus only 'benign' members (e.g. OperatingSegmentsMember). Returns
    (facts_by_(member, concept), discovered member counts).
    """
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    ctx: dict[str, tuple[str, str, list[str]]] = {}
    for c in root.iter(XBRLI + "context"):
        per = c.find(XBRLI + "period")
        if per is None:
            continue
        sd, ed = per.findtext(XBRLI + "startDate"), per.findtext(XBRLI + "endDate")
        if not sd or not ed:
            continue
        mems = [(m.text or "").strip().split(":")[-1] for m in c.iter(XBRLDI + "explicitMember")]
        ctx[c.get("id")] = (sd.strip(), ed.strip(), mems)

    facts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    discovered: dict[str, int] = defaultdict(int)
    for el in root:
        tag = el.tag
        if not isinstance(tag, str) or not tag.startswith("{"):
            continue
        local = tag.split("}", 1)[1]
        if local not in concepts:
            continue
        cref = el.get("contextRef")
        if cref not in ctx:
            continue
        sd, ed, mems = ctx[cref]
        for m in mems:
            if any(t.lower() in m.lower() for t in discovery_terms):
                discovered[m] += 1
        hits = [m for m in mems if m in members]
        others = [m for m in mems if m not in members]
        if len(hits) != 1 or any(o not in benign for o in others):
            continue
        try:
            val = float((el.text or "").strip())
        except ValueError:
            continue
        facts[(hits[0], local)].append({"start": sd, "end": ed, "val": val, "filed": filed})
    return facts, discovered


def fetch_segments(sec: SEC, ticker: str, meta: dict, cfg: dict) -> dict[str, float]:
    members = meta.get("segment_members") or []
    if not members:
        return {}
    scfg = cfg["segments"]
    sub = sec.json(f"https://data.sec.gov/submissions/CIK{meta['cik']:010d}.json")
    rec = sub["filings"]["recent"]
    rows = [(f, a, d, fd) for f, a, d, fd in zip(rec["form"], rec["accessionNumber"],
                                                  rec["primaryDocument"], rec["filingDate"])
            if f in ("10-Q", "10-K")][: scfg["max_filings"]]

    all_facts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    discovered: dict[str, int] = defaultdict(int)
    for form, acc, doc, filed in rows:
        base = f"https://www.sec.gov/Archives/edgar/data/{meta['cik']}/{acc.replace('-', '')}/"
        xml_text = None
        if doc.lower().endswith(".htm"):
            r = sec.get(base + doc[:-4] + "_htm.xml", allow_404=True)
            xml_text = r.text if r is not None else None
        if xml_text is None:
            idx = sec.json(base + "index.json")
            names = [i["name"] for i in idx.get("directory", {}).get("item", [])]
            cands = [n for n in names if n.endswith("_htm.xml")] or [
                n for n in names if n.endswith(".xml") and "_" in n
                and not n.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml"))
                and not n.startswith("FilingSummary")]
            if not cands:
                log("WARN", f"{ticker} {form} {acc}: no XBRL instance found")
                continue
            xml_text = sec.get(base + cands[0]).text
        try:
            f, disc = parse_instance(xml_text, scfg["revenue_concepts"], members,
                                     scfg["benign_members"], scfg["discovery_terms"], filed)
        except ET.ParseError as e:
            log("WARN", f"{ticker} {form} {acc}: XBRL parse error {e}")
            continue
        for k, v in f.items():
            all_facts[k].extend(v)
        for k, v in disc.items():
            discovered[k] += v

    if discovered:
        top = sorted(discovered.items(), key=lambda kv: -kv[1])[:8]
        log("INFO", f"{ticker} segment members seen: " + ", ".join(f"{k}({v})" for k, v in top))

    merged: dict[str, float] = {}
    for member in members:                      # member priority first
        for concept in scfg["revenue_concepts"]:  # then concept priority
            facts = all_facts.get((member, concept))
            if not facts:
                continue
            q, _m, iss = derive_quarterly(facts, cfg["min_year"])
            for i in iss:
                log("WARN", f"{ticker} segment {member} reconciliation {i}")
            for lab, v in q.items():
                merged.setdefault(lab, v)
    if merged:
        log("INFO", f"{ticker} segment revenue: {len(merged)} quarters, latest {max(merged)}")
    else:
        log("WARN", f"{ticker} segment revenue: none matched {members}. Check 'segment members seen' above.")
    return merged


# --------------------------------------------------------------------------
# Stage 3: markets
# --------------------------------------------------------------------------
def fetch_markets(cfg: dict) -> dict:
    import pandas as pd
    import yfinance as yf

    bench = cfg["market"]["benchmark"]
    tickers = [bench] + cfg["canary"]
    raw = yf.download(tickers, period="3y", auto_adjust=True, progress=False, threads=False)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
    close = close.dropna(subset=[bench])
    b = close[bench]
    out = {"as_of": str(close.index[-1].date()), "canaries": {}}
    ws, wl = cfg["market"]["window_short"], cfg["market"]["window_long"]
    for t in cfg["canary"]:
        if t not in close:
            log("WARN", f"market: no data for {t}")
            continue
        s = close[t].dropna()
        rel = (s / b.reindex(s.index)).dropna()
        if len(rel) < 2:
            continue
        rec = {"days": int(len(rel))}
        rec["rel_short"] = float(rel.iloc[-1] / rel.iloc[-1 - ws] - 1) if len(rel) > ws else None
        rec["rel_long"] = float(rel.iloc[-1] / rel.iloc[-1 - wl] - 1) if len(rel) > wl else None
        rec["rel_drawdown"] = float(rel.iloc[-1] / rel.rolling(252, min_periods=1).max().iloc[-1] - 1)
        wk = rel.resample("W-FRI").last().dropna().iloc[-104:]
        wk = wk / wk.iloc[0] * 100
        rec["weekly"] = {"dates": [str(d.date()) for d in wk.index], "values": [round(float(v), 2) for v in wk]}
        out["canaries"][t] = rec
        log("INFO", f"market {t} vs {bench}: {ws}d {rec['rel_short']}, drawdown {rec['rel_drawdown']:.3f}")
    return out


# --------------------------------------------------------------------------
# Stage 4: alerts (useful-life changes)
# --------------------------------------------------------------------------
def fetch_alerts(sec: SEC, cfg: dict) -> list[dict]:
    today = dt.date.today()
    start = today - dt.timedelta(days=cfg["alerts"]["lookback_days"])
    cik_to_ticker = {m["cik"]: t for t, m in cfg["companies"].items()}
    found: dict[str, dict] = {}
    for q in cfg["alerts"]["queries"]:
        for cik, ticker in cik_to_ticker.items():
            params = {"q": q, "forms": "10-K,10-Q", "dateRange": "custom",
                      "startdt": start.isoformat(), "enddt": today.isoformat(),
                      "ciks": f"{cik:010d}"}
            js = sec.json("https://efts.sec.gov/LATEST/search-index", params=params)
            for h in js.get("hits", {}).get("hits", []):
                src = h.get("_source", {})
                hit_ciks = {int(c) for c in src.get("ciks", []) if str(c).isdigit()}
                if cik not in hit_ciks:
                    continue  # server ignored the filter; keep only our companies
                adsh, _, fname = str(h.get("_id", "")).partition(":")
                key = f"{adsh}:{fname}"
                found.setdefault(key, {
                    "id": key, "ticker": ticker, "form": src.get("form") or src.get("file_type"),
                    "filed": src.get("file_date"), "query": q,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}",
                })
    log("INFO", f"alerts: {len(found)} matching filings in last {cfg['alerts']['lookback_days']} days")
    return list(found.values())


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------
def merge_immutable(old: dict, new: dict, path: str, restatements: list, accept: bool) -> dict:
    out = dict(old or {})
    for lab, v in new.items():
        if lab not in out:
            out[lab] = v
        elif not _close(v, out[lab], RESTATE_TOL):
            restatements.append({"series": path, "quarter": lab, "published": out[lab],
                                 "latest_filing": v, "applied": accept})
            if accept:
                out[lab] = v
    return out


# --------------------------------------------------------------------------
# Stage 5: signals
# --------------------------------------------------------------------------
def ttm(series: dict[str, float], lab: str):
    labs = [qoffset(lab, -i) for i in range(4)]
    return sum(series[x] for x in labs) if all(x in series for x in labs) else None


def agg(source: dict, tickers: list[str], metric: str | None = None) -> dict[str, float]:
    """Sum across tickers, only for quarters every ticker has."""
    def ser(t):
        node = source.get(t, {})
        return node.get(metric, {}) if metric else node
    if not tickers:
        return {}
    common = set.intersection(*[set(ser(t)) for t in tickers])
    return {lab: sum(ser(t)[lab] for t in tickers) for lab in common}


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def band(v, watch, stress, higher_is_worse=True):
    if v is None:
        return "nodata"
    if higher_is_worse:
        return "stress" if v >= stress else "watch" if v >= watch else "clear"
    return "stress" if v <= stress else "watch" if v <= watch else "clear"


def pct(v, digits=0, signed=False):
    if v is None:
        return "n/a"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def compute(fund: dict, seg: dict, mkt: dict, alerts: list, cfg: dict) -> dict:
    th = cfg["thresholds"]
    core, canary, supplier = cfg["core"], cfg["canary"], cfg["supplier"]

    C = agg(fund, core, "capex")
    O = agg(fund, core, "ocf")
    D = agg(fund, core, "dna")
    B = agg(fund, core, "total_debt")

    rows = []
    for lab in sorted(set(C) & set(O)):
        cap_t, ocf_t, dna_t = ttm(C, lab), ttm(O, lab), ttm(D, lab)
        if cap_t is None or ocf_t is None:
            continue
        debt_chg = (B[lab] - B[qoffset(lab, -4)]) if lab in B and qoffset(lab, -4) in B else None
        cap_prev = ttm(C, qoffset(lab, -4))
        rows.append({"q": lab, "capex_ttm": cap_t, "ocf_ttm": ocf_t,
                     "capex_ocf": ratio(cap_t, ocf_t), "capex_dna": ratio(cap_t, dna_t),
                     "debt_share": ratio(debt_chg, cap_t),
                     "capex_yoy": (cap_t / cap_prev - 1) if cap_prev else None})
    rows = rows[-16:]
    latest = rows[-1] if rows else {}
    lq = latest.get("q")

    def hist(key):
        return {"labels": [r["q"] for r in rows], "values": [None if r[key] is None else round(r[key], 4) for r in rows]}

    signals = []

    # 1. Capex / operating cash flow
    v1 = latest.get("capex_ocf")
    prior = rows[-3]["capex_ocf"] if len(rows) >= 3 else None
    direction = None if v1 is None or prior is None else ("rising" if v1 > prior else "falling")
    canary_lines = []
    for t in canary:
        cc, oc = fund.get(t, {}).get("capex", {}), fund.get(t, {}).get("ocf", {})
        labs = sorted(set(cc) & set(oc))
        if labs:
            ct, ot = ttm(cc, labs[-1]), ttm(oc, labs[-1])
            if ct is not None and ot is not None:
                canary_lines.append(f"{t} {'OCF negative' if ot <= 0 else f'{ct / ot:.2f}x'} ({labs[-1]})")
    signals.append({
        "rank": 1, "key": "capex_ocf", "name": "Capex vs. operating cash flow",
        "reads": "Above 100%, the buildout needs outside money to continue.",
        "reading_text": "n/a" if v1 is None else f"{v1 * 100:.0f}%",
        "state": band(v1, th["capex_ocf"]["watch"], th["capex_ocf"]["stress"]),
        "detail": ("Core hyperscalers, trailing 12 months" + (f", {direction} vs. two quarters ago" if direction else "")
                   + (". Canaries: " + "; ".join(canary_lines) if canary_lines else "")),
        "chart": hist("capex_ocf"), "threshold": th["capex_ocf"]["stress"], "fmt": "pct"})

    # 2. Canary market stress
    worst, worst_t = None, None
    lines = []
    for t, rec in (mkt or {}).get("canaries", {}).items():
        r = rec.get("rel_short")
        lines.append(f"{t} {pct(r, signed=True)} vs. QQQ, {pct(rec.get('rel_drawdown'), signed=True)} from relative high")
        if r is not None and (worst is None or r < worst):
            worst, worst_t = r, t
    wk = (mkt or {}).get("canaries", {}).get(worst_t, {}).get("weekly") if worst_t else None
    signals.append({
        "rank": 2, "key": "canary_market", "name": "Debt-funded builders vs. the market",
        "reads": "Markets price funding stress at the levered edge before filings show it.",
        "reading_text": "n/a" if worst is None else f"{pct(worst, signed=True)} {worst_t}",
        "state": band(worst, th["canary_rel_63d"]["watch"], th["canary_rel_63d"]["stress"], higher_is_worse=False),
        "detail": (f"Worst {cfg['market']['window_short']}-day relative return. " + "; ".join(lines)
                   + (f". Prices as of {mkt.get('as_of')}" if mkt else "")) if lines else "No market data this run.",
        "chart": {"labels": wk["dates"], "values": wk["values"]} if wk else None,
        "threshold": None, "fmt": "index"})

    # 3. Debt share of capex
    v3 = latest.get("debt_share")
    rising2 = (len(rows) >= 3 and all(rows[i]["debt_share"] is not None for i in (-1, -2, -3))
               and rows[-1]["debt_share"] > rows[-2]["debt_share"] > rows[-3]["debt_share"])
    s3 = band(v3, th["debt_share"]["watch"], th["debt_share"]["stress"])
    canary_debt = []
    for t in canary:
        td = fund.get(t, {}).get("total_debt", {})
        if td:
            lab = max(td)
            prev = td.get(qoffset(lab, -4))
            if prev:
                canary_debt.append(f"{t} debt {pct(td[lab] / prev - 1, signed=True)} y/y")
    signals.append({
        "rank": 3, "key": "debt_share", "name": "Share of capex funded by new debt",
        "reads": "Confirms signal 1: is borrowing filling the gap?",
        "reading_text": "n/a" if v3 is None else pct(v3),
        "state": s3,
        "detail": ("Change in core gross debt over 12 months as a share of 12-month capex"
                   + (", rising two quarters running" if rising2 else "")
                   + (". " + "; ".join(canary_debt) if canary_debt else "")
                   + ". Excludes leases and off-balance-sheet vehicles; see filings below."),
        "chart": hist("debt_share"), "threshold": th["debt_share"]["stress"], "fmt": "pct"})

    # 4. Supplier second derivative (NVIDIA)
    s4, text4, detail4, chart4 = "nodata", "n/a", "No supplier data.", None
    for t in supplier:
        f = fund.get(t, {})
        dc = seg.get(t) if len(seg.get(t, {})) >= 6 else None
        src = "data center segment" if dc else "total revenue (segment not found)"
        dc = dc or f.get("revenue", {})
        labs = sorted(dc)
        yoy = {l: dc[l] / dc[qoffset(l, -4)] - 1 for l in labs if dc.get(qoffset(l, -4))}
        yl = sorted(yoy)
        if len(yl) < 3:
            continue
        decel2 = yoy[yl[-1]] < yoy[yl[-2]] < yoy[yl[-3]]
        inv, cogs, rec, rev = f.get("inventory", {}), f.get("cogs", {}), f.get("receivables", {}), f.get("revenue", {})
        dio = {l: inv[l] / cogs[l] * 91 for l in inv if cogs.get(l)}
        dso = {l: rec[l] / rev[l] * 91 for l in rec if rev.get(l)}
        dl = sorted(dio)
        dio_up2 = len(dl) >= 3 and dio[dl[-1]] > dio[dl[-2]] > dio[dl[-3]]
        s4 = "stress" if decel2 and dio_up2 else "watch" if decel2 or dio_up2 else "clear"
        text4 = f"{pct(yoy[yl[-1]], signed=True)} y/y"
        sl = sorted(dso)
        detail4 = (f"{t} {src} growth, {yl[-1]}. "
                   + ("Decelerating two quarters running. " if decel2 else "Not decelerating two quarters running. ")
                   + (f"Inventory days {dio[dl[-1]]:.0f}" + (" and rising." if dio_up2 else ".") if dl else "")
                   + (f" Receivable days {dso[sl[-1]]:.0f}." if sl else ""))
        yl16 = yl[-12:]
        chart4 = {"labels": yl16, "values": [round(yoy[l], 4) for l in yl16]}
    signals.append({"rank": 4, "key": "supplier", "name": "Supplier growth and inventory",
                    "reads": "The earliest fundamental sign that orders are slowing or being stretched.",
                    "reading_text": text4, "state": s4, "detail": detail4,
                    "chart": chart4, "threshold": None, "fmt": "pct"})

    # 5. Cloud revenue growth vs. capex growth (like-for-like companies)
    cloud_t = [t for t in core if cfg["companies"][t].get("segment_members")]
    S = agg(seg, cloud_t)
    Cc = agg(fund, cloud_t, "capex")
    gaps = []
    for lab in sorted(S):
        a, a0 = ttm(S, lab), ttm(S, qoffset(lab, -4))
        b, b0 = ttm(Cc, lab), ttm(Cc, qoffset(lab, -4))
        if None in (a, a0, b, b0) or 0 in (a0, b0):
            continue
        gaps.append((lab, a / a0 - 1, b / b0 - 1))
    s5, text5, detail5, chart5 = "nodata", "n/a", "Cloud segment revenue not available yet.", None
    if gaps:
        run = 0
        for _, g, c in reversed(gaps):
            if g < c:
                run += 1
            else:
                break
        lab, g, c = gaps[-1]
        s5 = "watch" if run >= th["cloud_gap_quarters"]["watch"] else "clear"
        text5 = f"{(g - c) * 100:+.0f} pts"
        detail5 = (f"Cloud revenue {pct(g, signed=True)} vs. capex {pct(c, signed=True)}, trailing 12 months y/y, {lab} "
                   f"({', '.join(cloud_t)}). Revenue has trailed capex {run} quarter{'s' if run != 1 else ''} running. "
                   "A gap alone means an early buildout, not failure, so this signal caps at watch.")
        g12 = gaps[-12:]
        chart5 = {"labels": [x[0] for x in g12], "values": [round(x[1] - x[2], 4) for x in g12]}
    signals.append({"rank": 5, "key": "cloud_vs_capex", "name": "Cloud revenue growth vs. capex growth",
                    "reads": "The central question, but a gap is normal early in a buildout.",
                    "reading_text": text5, "state": s5, "detail": detail5,
                    "chart": chart5, "threshold": 0, "fmt": "pts"})

    # 6. Capex / depreciation
    v6 = latest.get("capex_dna")
    signals.append({"rank": 6, "key": "capex_dna", "name": "Capex vs. depreciation",
                    "reads": "Not a stress detector: shows how large the coming depreciation wave is.",
                    "reading_text": "n/a" if v6 is None else f"{v6:.1f}x", "state": "info" if v6 else "nodata",
                    "detail": "Core hyperscalers, trailing 12 months. The higher this runs, the more future earnings absorb today's spending.",
                    "chart": hist("capex_dna"), "threshold": None, "fmt": "x"})

    # 7. GPU rental pricing (phase 2)
    signals.append({"rank": 7, "key": "gpu_pricing", "name": "GPU rental pricing",
                    "reads": "Real-time oversupply signal. Coming in phase 2.",
                    "reading_text": "Phase 2", "state": "untracked",
                    "detail": "Planned: weekly H100 and B200 hourly price snapshot, tracked by generation.",
                    "chart": None, "threshold": None, "fmt": None})

    # 8. Useful-life change disclosures
    cutoff = (dt.date.today() - dt.timedelta(days=cfg["alerts"]["lookback_days"])).isoformat()
    recent = [a for a in alerts if str(a.get("filed", "")) >= cutoff]
    signals.append({"rank": 8, "key": "useful_life", "name": "Depreciation-life changes",
                    "reads": "Extending lives flatters earnings; shortening concedes faster obsolescence.",
                    "reading_text": f"{len(recent)} filing{'s' if len(recent) != 1 else ''}",
                    "state": "watch" if recent else "clear",
                    "detail": "Filings in the lookback window that mention a change in estimated useful lives. Read them: a match is a prompt, not a verdict.",
                    "chart": None, "threshold": None, "fmt": None})

    # Overall status
    st = {s["key"]: s["state"] for s in signals}
    warnish = ("watch", "stress")
    if st["capex_ocf"] == "nodata" or st["debt_share"] == "nodata":
        status = "incomplete"
    elif st["capex_ocf"] == "stress" and st["debt_share"] == "stress":
        status = "stress"
    elif sum(st[k] in warnish for k in ("capex_ocf", "canary_market", "debt_share")) >= 2:
        status = "elevated"
    else:
        status = "normal"

    return {"status": status, "latest_quarter": lq, "signals": signals, "history": rows}


# --------------------------------------------------------------------------
# Stage 6: publish
# --------------------------------------------------------------------------
def write_log():
    DOCS.mkdir(exist_ok=True)
    LOG_PATH.write_text("\n".join(LOG_LINES) + "\n")


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    prev = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {}
    accept = bool(cfg.get("accept_restatements"))
    log("INFO", f"run start; previous publish {prev.get('generated_utc', 'none')}")

    ua = os.environ.get(cfg["sec_user_agent_env"], "").strip()
    if "@" not in ua:
        log("ERROR", f"Secret {cfg['sec_user_agent_env']} missing or has no email. SEC requires "
                     "'Company Name contact@domain'. Add it under Settings > Secrets and variables > Actions.")
        write_log()
        return 1
    sec = SEC(ua)

    tickers = cfg["core"] + cfg["canary"] + cfg["supplier"]
    failures: list[str] = []
    restatements: list[dict] = []
    recon: list[dict] = []
    concepts_used: dict = {}

    fund = {t: dict(v) for t, v in prev.get("fundamentals", {}).items()}
    for t in tickers:
        try:
            series, used, issues = fetch_fundamentals(sec, t, cfg["companies"][t], cfg)
        except Exception as e:
            log("ERROR", f"{t} fundamentals failed: {e}")
            failures.append(t)
            continue
        concepts_used[t] = used
        recon.extend(issues)
        node = fund.setdefault(t, {})
        for m, s in series.items():
            node[m] = merge_immutable(node.get(m, {}), s, f"{t}.{m}", restatements, accept)

    seg = dict(prev.get("segments", {}))
    for t in tickers:
        meta = cfg["companies"][t]
        if not meta.get("segment_members"):
            continue
        try:
            s = fetch_segments(sec, t, meta, cfg)
            seg[t] = merge_immutable(seg.get(t, {}), s, f"{t}.segment", restatements, accept)
        except Exception as e:
            log("WARN", f"{t} segments failed: {e}")

    try:
        mkt = fetch_markets(cfg)
    except Exception as e:
        log("WARN", f"markets failed, reusing last snapshot: {e}")
        mkt = prev.get("markets", {})

    alerts = {a["id"]: a for a in prev.get("alerts", [])}
    try:
        for a in fetch_alerts(sec, cfg):
            alerts.setdefault(a["id"], {**a, "first_seen": dt.date.today().isoformat()})
    except Exception as e:
        log("WARN", f"alerts failed, keeping previous list: {e}")
    alerts_list = sorted(alerts.values(), key=lambda a: str(a.get("filed", "")), reverse=True)

    # Completeness gate: never publish a core aggregate built on a missing company.
    core_failed = [t for t in cfg["core"] if t in failures]
    if core_failed:
        log("ERROR", f"Completeness gate: core fetch failed for {core_failed}. Last publish left untouched.")
        write_log()
        return 1

    result = compute(fund, seg, mkt, alerts_list, cfg)
    for r in restatements:
        log("WARN", f"restatement {r['series']} {r['quarter']}: published {r['published']:.0f}, "
                    f"latest filing {r['latest_filing']:.0f} ({'applied' if r['applied'] else 'kept published'})")

    freshness = {}
    for t in tickers:
        caps = fund.get(t, {}).get("capex") or fund.get(t, {}).get("revenue") or {}
        freshness[t] = max(caps) if caps else None

    out = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **result,
        "freshness": freshness,
        "failures": failures,
        "restatements": restatements,
        "reconciliation": recon,
        "concepts_used": concepts_used,
        "alerts": alerts_list,
        "markets": mkt,
        "segments": seg,
        "fundamentals": fund,
    }
    DOCS.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, indent=1, default=str))
    log("INFO", f"published: status {result['status']}, latest quarter {result['latest_quarter']}, "
                f"{len(failures)} non-core failures, {len(restatements)} restatements, {len(recon)} reconciliation issues")
    write_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
