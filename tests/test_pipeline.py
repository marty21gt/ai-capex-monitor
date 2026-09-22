"""Offline tests. Run before every pipeline execution; the workflow stops if any fail."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline as p  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def F(start, end, val, filed="2025-01-01"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-Q"}


# ---------------------------------------------------------------- quarter labels
def test_qlabel_calendar_quarters():
    assert p.qlabel(dt.date(2025, 3, 31)) == "2025Q1"
    assert p.qlabel(dt.date(2025, 12, 31)) == "2025Q4"


def test_qlabel_off_calendar_fiscal_quarters():
    assert p.qlabel(dt.date(2025, 8, 31)) == "2025Q3"   # ORCL fiscal Q1
    assert p.qlabel(dt.date(2025, 11, 30)) == "2025Q4"  # ORCL fiscal Q2
    assert p.qlabel(dt.date(2025, 10, 26)) == "2025Q3"  # NVDA fiscal Q3
    assert p.qlabel(dt.date(2026, 1, 25)) == "2025Q4"   # NVDA fiscal year end


def test_qoffset_wraps_years():
    assert p.qoffset("2025Q1", -1) == "2024Q4"
    assert p.qoffset("2025Q4", 1) == "2026Q1"
    assert p.qoffset("2025Q2", -4) == "2024Q2"


# ---------------------------------------------------------------- YTD differencing
def test_ytd_cash_flow_is_differenced_into_quarters():
    # Cash flow reported YTD only: Q1=10, H1=25, 9M=45, FY=70 -> quarters 10,15,20,25
    facts = [F("2024-01-01", "2024-03-31", 10), F("2024-01-01", "2024-06-30", 25),
             F("2024-01-01", "2024-09-30", 45), F("2024-01-01", "2024-12-31", 70)]
    q, m, issues = p.derive_quarterly(facts)
    assert q == {"2024Q1": 10, "2024Q2": 15, "2024Q3": 20, "2024Q4": 25}
    assert m["2024Q1"] == "direct" and m["2024Q3"] == "ytd_difference"
    assert issues == []


def test_microsoft_style_fiscal_year_july_start():
    facts = [F("2024-07-01", "2024-09-30", 20), F("2024-07-01", "2024-12-31", 42),
             F("2024-07-01", "2025-03-31", 63), F("2024-07-01", "2025-06-30", 88)]
    q, _, _ = p.derive_quarterly(facts)
    assert q == {"2024Q3": 20, "2024Q4": 22, "2025Q1": 21, "2025Q2": 25}


def test_q4_from_annual_when_nine_month_missing():
    facts = [F("2024-01-01", "2024-03-31", 10), F("2024-04-01", "2024-06-30", 12),
             F("2024-07-01", "2024-09-30", 14), F("2024-01-01", "2024-12-31", 50)]
    q, m, _ = p.derive_quarterly(facts)
    assert q["2024Q4"] == 14 and m["2024Q4"] == "annual_minus_3q"


def test_nvda_53_week_quarter_accepted():
    # 14-week fiscal quarter (98 days) must still count as a quarter
    facts = [F("2024-10-28", "2025-01-26", 5), F("2024-01-29", "2025-01-26", 20),
             F("2024-01-29", "2024-10-27", 15)]
    q, _, issues = p.derive_quarterly(facts)
    assert q["2024Q4"] == 5 and issues == []


def test_reconciliation_flags_direct_vs_derived_mismatch():
    facts = [F("2024-01-01", "2024-03-31", 10), F("2024-01-01", "2024-06-30", 25),
             F("2024-04-01", "2024-06-30", 18)]  # direct Q2 says 18, derived says 15
    q, _, issues = p.derive_quarterly(facts)
    assert q["2024Q2"] == 18
    assert len(issues) == 1 and issues[0]["quarter"] == "2024Q2"


def test_latest_filing_wins_for_same_period():
    facts = [F("2024-01-01", "2024-03-31", 10, filed="2024-05-01"),
             F("2024-01-01", "2024-03-31", 11, filed="2025-05-01")]
    q, _, _ = p.derive_quarterly(facts)
    assert q["2024Q1"] == 11


def test_instant_uses_last_date_in_quarter():
    facts = [{"end": "2024-03-31", "val": 100, "filed": "2024-05-01"},
             {"end": "2024-02-15", "val": 90, "filed": "2024-05-01"}]
    assert p.instant_quarterly(facts) == {"2024Q1": 100}


def test_total_debt_prefers_combined_concept():
    m = {"debt_lt": {"2024Q1": 100.0}, "debt_lt_noncurrent": {"2024Q1": 80.0, "2024Q2": 90.0},
         "debt_lt_current": {"2024Q2": 5.0}, "commercial_paper": {"2024Q1": 10.0}}
    td = p.total_debt(m)
    assert td == {"2024Q1": 110.0, "2024Q2": 95.0}


# ---------------------------------------------------------------- XBRL segments
INSTANCE = """<?xml version="1.0"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2024" xmlns:amzn="http://amazon.com/2024">
 <context id="c1"><entity><identifier scheme="x">1</identifier><segment>
   <xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">amzn:AmazonWebServicesSegmentMember</xbrldi:explicitMember>
 </segment></entity><period><startDate>2025-04-01</startDate><endDate>2025-06-30</endDate></period></context>
 <context id="c2"><entity><identifier scheme="x">1</identifier><segment>
   <xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">amzn:AmazonWebServicesSegmentMember</xbrldi:explicitMember>
   <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">amzn:SubscriptionMember</xbrldi:explicitMember>
 </segment></entity><period><startDate>2025-04-01</startDate><endDate>2025-06-30</endDate></period></context>
 <context id="c3"><entity><identifier scheme="x">1</identifier></entity>
   <period><startDate>2025-04-01</startDate><endDate>2025-06-30</endDate></period></context>
 <us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="c1" unitRef="usd" decimals="-6">30873000000</us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax>
 <us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="c2" unitRef="usd" decimals="-6">999</us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax>
 <us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="c3" unitRef="usd" decimals="-6">167702000000</us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax>
</xbrl>"""


def test_parse_instance_takes_only_clean_segment_context():
    facts, disc = p.parse_instance(INSTANCE, CFG["segments"]["revenue_concepts"],
                                   ["AmazonWebServicesSegmentMember"], CFG["segments"]["benign_members"],
                                   CFG["segments"]["discovery_terms"], "2025-08-01")
    vals = facts[("AmazonWebServicesSegmentMember", "RevenueFromContractWithCustomerExcludingAssessedTax")]
    assert [f["val"] for f in vals] == [30873000000.0]  # c2 (extra product member) and c3 (no member) excluded
    assert disc["AmazonWebServicesSegmentMember"] == 2


# ---------------------------------------------------------------- immutability
def test_restatement_kept_published_by_default():
    rs = []
    out = p.merge_immutable({"2024Q1": 100.0}, {"2024Q1": 110.0, "2024Q2": 50.0}, "X.capex", rs, accept=False)
    assert out == {"2024Q1": 100.0, "2024Q2": 50.0}
    assert len(rs) == 1 and rs[0]["applied"] is False


def test_rounding_noise_is_not_a_restatement():
    rs = []
    p.merge_immutable({"2024Q1": 100.0}, {"2024Q1": 100.2}, "X.capex", rs, accept=False)
    assert rs == []


# ---------------------------------------------------------------- signals
def _quarters(n, start="2022Q1"):
    return [p.qoffset(start, i) for i in range(n)]


def _fund(capex_path, ocf=100.0, debt_path=None):
    labs = _quarters(len(capex_path))
    debt_path = debt_path or [1000.0] * len(labs)
    one = {"capex": dict(zip(labs, capex_path)), "ocf": {l: ocf for l in labs},
           "dna": {l: 20.0 for l in labs}, "total_debt": dict(zip(labs, debt_path)),
           "revenue": {l: 500.0 for l in labs}}
    return {t: json.loads(json.dumps(one)) for t in CFG["core"]}


def test_self_funded_buildout_is_normal():
    r = p.compute(_fund([50.0] * 12), {}, {}, [], CFG)
    s = {x["key"]: x for x in r["signals"]}
    assert s["capex_ocf"]["state"] == "clear" and r["status"] == "normal"
    assert [x["rank"] for x in r["signals"]] == list(range(1, 9))


def test_debt_funded_overrun_is_stress():
    capex = [60.0] * 4 + [110.0] * 8
    debt = [1000.0 + 40.0 * i for i in range(12)]  # +160/yr vs TTM capex 440 -> 36%
    r = p.compute(_fund(capex, debt_path=debt), {}, {}, [], CFG)
    s = {x["key"]: x for x in r["signals"]}
    assert s["capex_ocf"]["state"] == "stress"
    assert s["debt_share"]["state"] == "stress"
    assert r["status"] == "stress"


def test_capex_overrun_without_debt_is_elevated_not_stress():
    capex = [60.0] * 4 + [110.0] * 8
    mkt = {"as_of": "2026-09-18", "canaries": {"CRWV": {"rel_short": -0.20, "rel_drawdown": -0.3}}}
    r = p.compute(_fund(capex), {}, mkt, [], CFG)
    assert r["status"] == "elevated"


def test_missing_core_data_is_incomplete():
    r = p.compute({}, {}, {}, [], CFG)
    assert r["status"] == "incomplete"
