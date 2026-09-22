# AI capex stress monitor

Tracks whether AI capex is overwhelming the growth it is meant to fund. Discretionary context only: nothing here feeds the fragility gate.

## Signals, ranked

1. **Capex ÷ operating cash flow** (core: MSFT, GOOGL, AMZN, META). Above 100% means the buildout needs outside money.
2. **Canary market stress.** ORCL and CRWV 63-day return relative to QQQ. Fastest signal, noisiest.
3. **New debt ÷ capex.** 12-month change in core gross debt as a share of 12-month capex. Confirms #1.
4. **Supplier second derivative.** NVDA data center growth decelerating two quarters running, plus inventory days rising.
5. **Cloud revenue vs. capex growth** (MSFT, GOOGL, AMZN, like-for-like). Caps at watch: a gap alone means an early buildout.
6. **Capex ÷ depreciation.** Context: size of the coming depreciation wave.
7. **GPU rental pricing.** Phase 2.
8. **Useful-life changes.** EDGAR full-text alerts.

**Overall status:** stress when #1 and #3 both breach; elevated when two of #1–#3 are at watch or worse; otherwise normal. Thresholds live in `config.json` and are judgment calls, not validated.

## Data integrity rules

- Cash-flow facts are year-to-date in filings. Quarters are derived by differencing facts that share a start date, reconciled against any directly reported 3-month value (1% tolerance). Q4 falls back to FY minus three quarters.
- Off-calendar fiscal quarters map to calendar quarters by period end minus 45 days (ORCL Aug 31 → Q3, NVDA late Oct → Q3).
- Each company's CIK is checked against its registered name every run.
- **Published quarters are immutable.** A later filing with a different value is listed under Data checks and not applied unless `"accept_restatements": true`.
- **Completeness gate:** if any core company fails to fetch, nothing is published and the last good `data.json` stays live.
- Offline tests run before every pipeline execution; a failing test stops the run.

## Setup (GitHub web UI)

1. Create a new repo, e.g. `ai-capex-monitor`.
2. **Add file → Upload files**: drag in `pipeline.py`, `config.json`, `requirements.txt`, `README.md`, and the `docs` and `tests` folders. Commit.
3. The workflow sits in a hidden folder that Finder may not show, so create it directly: **Add file → Create new file**, name it `.github/workflows/update.yml`, paste the contents, commit.
4. **Settings → Secrets and variables → Actions → New repository secret**: name `SEC_USER_AGENT`, value `Stadium Financial your-email@yourdomain.com`. The SEC requires an identifying User-Agent with a contact email.
5. **Settings → Actions → General → Workflow permissions**: Read and write.
6. **Settings → Pages**: deploy from branch `main`, folder `/docs`.
7. **Actions → Update monitor → Run workflow.**

Scheduled runs: Mondays and Thursdays, 12:30 UTC.

## First-run checklist

The code was tested offline against synthetic filings; the first live run is the real test. Open `docs/run_log.txt` (or the Run log panel on the page) and check:

- **Each company line:** the entity name matches, the latest quarter is recent, and the capex and D&A concepts look right.
- **"segment members seen" lines:** if a segment shows "none matched", copy the correct member name from the log into `segment_members` in `config.json`. Member names change when companies reorganize segments.
- **Reconciliation warnings:** any mismatch between derived and direct quarters means a concept or period problem to fix before trusting the numbers.
- **Sanity check one number by hand:** compare the 12-month capex on the meter against the sum of the last four quarters from one company's earnings release.

If the first publish contains a bad quarter, fix the config, delete `docs/data.json`, and rerun. Immutability only applies once you accept a publish.

## Known limits

- Gross debt excludes lease liabilities and off-balance-sheet vehicles (JVs, SPVs). The alert searches are the partial backstop.
- Cloud segments include non-AI revenue; META has no cloud segment and is excluded from #5.
- Quarterly filings lag one to two months. In a real unwind, the fragility gate will almost certainly move before this page confirms anything.
