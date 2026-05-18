# Performance methodology (current as of 2026-05-18)

The dashboard's primary "Performance vs benchmarks" view uses
**position-alpha** as of this audit. The legacy Modified Dietz + matched-
flow approach is documented at the bottom for reference, but the
dashboard no longer renders it.

This document is intentionally blunt about what the pipeline can and
cannot tell you. The "Known faults" section is the part you should
read carefully — anything else is bookkeeping.

---

## 0. Top-line on H2 2025

For a 1Y window (May 18 2025 → May 18 2026), with the latest broker
data refreshed:

| | Value |
|---|---|
| V_start (positions only, no cash) | $432,779 |
| V_end | $606,740 |
| Total Actual P&L | +$116,453 |
| SPY counterfactual P&L | +$106,703 |
| QQQ counterfactual P&L | +$160,010 |
| Policy counterfactual P&L | +$103,220 |
| **Alpha vs SPY** | **+$9,749** |
| Alpha vs QQQ | −$43,558 |
| Alpha vs Policy | +$13,233 |

For a 2Y window (May 18 2024 → May 18 2026), V_start re-anchors to
$346,359, total alpha vs SPY is +$11,442. Switching the chart window
re-baselines V_start to the value at that window's start date.

---

## 1. Methodology — position-alpha

### Idea

The user's portfolio at any window-start date is a fresh balance
sheet. For each ticker the user held on that day, the starting capital
is `qty_at_start × price_at_start`. We then ask:

> "Holding aside cash. If I'd instead put `qty_at_start × price_at_start`
> in SPY at that day's SPY close, and then every $ I subsequently
> bought of this ticker had instead gone into SPY, and every $ I sold
> of this ticker had instead come from selling SPY — what would I have
> ended the window with?"

Alpha is the difference between actual position value at window-end
and that SPY counterfactual value, summed across all tickers.

The same calculation runs against QQQ and the user's POLICY basket as
alternative benchmarks. The dashboard renders all three lines and
lets the user toggle them.

### Algorithm

For each ticker held in the window:
1. `V_start_ticker = qty_at_start × price_at_start`
2. Initialize `spy_shares_ticker = V_start_ticker / SPY[start_date]`
3. For each in-window BUY of $X on day d:
   - `spy_shares_ticker += X / SPY[d]`
4. For each in-window SELL of $Y on day d:
   - `spy_shares_ticker -= Y / SPY[d]`
5. `V_end_spy_ticker = spy_shares_ticker × SPY[end_date]`
6. Actual P&L = `V_end + sold − bought − V_start`
7. SPY P&L = `V_end_spy + sold − bought − V_start`
8. Alpha = Actual P&L − SPY P&L = `V_end − V_end_spy`

The aggregate dashboard chart plots three lines: portfolio sum, SPY
sum, QQQ sum, and policy basket sum — all per-day, anchored to the
same V_start.

### Why this is more honest than Modified Dietz

| Property | Position-alpha | Modified Dietz (old) |
|---|---|---|
| Window start | Fresh balance sheet — V_start anchors to that date's qty × price | Reconstructed via walk-back; sensitive to pre-window transaction completeness |
| Cash treatment | Cash is excluded entirely (it's not a position) | Cash is part of V; opportunity-cost of holding cash counts vs SPY |
| Cashflow impact on returns | None — buys/sells are internal to the per-ticker comparison | Inflated denominator from contribution timing; can swing % return by ±10pp |
| Pre-history positions | Handled naturally — V_start uses today's qty × that day's price | Phantom alpha when sells exceed buys (pre-window holdings are realized as "gains") |
| Unit | Dollars (legible) | Percent of weighted capital (hard to interpret with heavy contributions) |
| Per-ticker decomposition | Yes — each row contributes a $ alpha that sums to the total | No |

The position-alpha number is the one we now treat as canonical.

### Code paths

- `services/position_alpha.py:compute_position_alpha` — the entry
  point; called from `GET /api/portfolio/position-alpha`.
- `services/position_alpha.py:_qty_per_ticker_at_date` — walk-back
  helper to determine qty at any date, anchored to the earliest broker
  snapshot.
- `services/position_alpha.py:_compute_alpha_series` — builds the
  daily chart time series.
- `components/PositionAlphaChart.tsx` + `PositionAlphaTable.tsx` —
  rendering.

---

## 2. Transaction classification rules

The classification heuristic only matters for the **legacy**
performance/Risk-Metrics queries (which use external cashflows in
their denominator). Position-alpha doesn't use cashflow classifications
at all — it derives everything from positions and per-trade dollar
amounts.

Precedence in `services/performance.py:_signed_cashflow`:

1. **Explicit override** (`transaction_overrides` table) — never
   overridden by anything else. Values: `external_in`, `external_out`,
   `internal`.
2. **Name-hint** (only for `type ∈ {cash, transfer}`):
   - "reinvestment" / "drip" → `internal`
   - "dividend" / "interest payment" / "credit interest" → `internal`
   - "outgoing" / "withdrawal" → `external_out`
   - "incoming" / "deposit" → `external_in`
3. **Subtype heuristic** — final fallback. See the source for the
   full subtype map.

The user has applied ~120 overrides covering withdrawals (treated as
external_out unless reinvested within 30 days), 401(k) contributions
(external_in), DRIP buys (internal), and the Dec 23 2025 transfer
(external_in, per the user's intent).

---

## 3. Known faults — what could go wrong with position-alpha

### F1 — Zero broker snapshots before 2026-05-09 [SEVERE for old dates]

Every chart point before May 9 2026 is reconstructed via walk-back.
There is no broker-verified V on any historical date.

`qty_at_start` for a pre-anchor date comes from walking transactions
backward from the May-9-anchor. Errors compound when:
- The transaction log is incomplete (missing pre-window buys → can't
  reconstruct the original lot)
- Quantity sign conventions disagree across aggregators (handled
  case-by-case but not bulletproof)

**Mitigation in code**: position-alpha is more robust to walk-back
errors than Modified Dietz because:
- We use qty × price at start (a real, sensible number) instead of
  reconstructing dollar V (which compounds cash_adj errors)
- Pre-history shares get correctly anchored at market value on day 0
  (no "phantom" cost-basis distortion)

### F2 — Pre-history positions are handled cleanly (NOT a fault) [REVISED]

Earlier audit drafts called out pre-history positions as a Trade
Analysis P&L distortion. With position-alpha that's now NOT a
problem for the chart: a pre-history holding starts the window at its
window-start market value, and the SPY counterfactual is anchored to
that same value. The "missing buys" only affect lifetime cost-basis
reporting in the Trade Analysis page — they don't affect alpha.

The Trade Analysis page still uses lifetime aggregates and IS
distorted by pre-history positions (see F7). The dashboard's
position-alpha view is unaffected.

### F3 — Securities without yfinance/stooq history fall back to today's `institution_price` [MILD]

When a security has no `prices` row for a historical date, the
backfill engine uses the most recent `holdings_snapshots.institution_price`
as a proxy. That's TODAY's price applied to a historical date.

Effect: those tickers show a flat line at current price across the
backfilled window, missing intra-period movement. For an illiquid
foreign stock that doubled mid-window, V at the start of the window
is overstated.

Position-alpha is also vulnerable here. The fix would be a Prices
admin UI to surface tickers with no price history.

### F4 — `active_account_ids` filter drops history of disabled items [MILD currently]

Items with `is_data_active=0` are excluded from every query. If you
disable an item later, its history disappears retroactively.

Current state: only Schwab (item 7, manual) is inactive, with 0
transactions — so this is not currently biting your data. Future risk
if you disable an active item.

### F5 — Broker sign convention disagreement [LARGELY MITIGATED]

Plaid signs `amount` from the cash account's perspective (buy
positive, sell negative). SnapTrade signs the opposite way. The
position-alpha service uses `|amount|` and the transaction TYPE to
determine direction — so the sign convention disagreement doesn't
distort the calculation.

### F6 — Risk Metrics alpha disagrees with position-alpha [SUBSTANTIAL]

The `RiskMetricsCard` shows alpha-annualized in the range of
−16% to −30% depending on toggles. Position-alpha shows dollar
alpha of +$4-10k for the same windows.

**Why they disagree**: the Risk Metrics card runs an OLS regression
on **daily returns** computed from `_daily_portfolio_value` (which
includes cash, the SGOV reserve, broad-index ETFs, and the walk-back
`cash_adj` term). When the same regression is run on the
position-alpha V series instead, alpha is +0.70% annualized — close to
zero, consistent with the dollar number.

The Risk Metrics calculation is mathematically correct given its
inputs, but its daily-return series is noisy from cashflow attribution
smoothing in the walk-back. **The position-alpha dollar number is the
one to trust.** The Risk Metrics card needs to be updated to derive
its daily-return series from the position-alpha V series; until then,
the Risk Metrics alpha/Sharpe/Sortino numbers should be read with a
large grain of salt.

Beta and R² from the regression remain useful as relative measures
(does the portfolio's daily motion track SPY's?). The alpha INTERCEPT
is the misleading part.

### F7 — Trade Analysis P&L still uses lifetime aggregates [SUBSTANTIAL for some rows]

The `Trade Analysis` page (separate from the dashboard) computes
per-ticker P&L as `today_value + sold − bought` across LIFETIME. For
36+ tickers with pre-history sells, this overstates the apparent P&L
because `bought` doesn't include pre-window purchases.

Tickers affected (apparent P&L vs reality):
- VTI: shows +$206k (just realization of pre-history holdings)
- JPM, BABA, AMZN, GOOG, SOFI, MU: $10-30k each overstated
- 30+ smaller positions

The position-alpha view on the dashboard ISN'T affected — the
windowed methodology handles pre-history shares correctly.

**Recommendation**: deprecate the Trade Analysis dollar-P&L column,
or migrate it to use the windowed position-alpha methodology.

### F8 — Snapshots include cash equivalents but position-alpha skips them [BY DESIGN]

Position-alpha skips `_CASH_EQUIV_TICKERS = {SGOV, FDRXX, SHV,
SPAXX, CUR:USD, VMFXX}`. SGOV is a short-term Treasury ETF that the
user holds as an emergency reserve.

Consequence: the **"Excl. $30k SGOV reserve" toggle is a no-op for
position-alpha** because SGOV is already excluded. The toggle still
affects the legacy performance/Risk-Metrics queries; tooltip on the
dashboard reflects this.

If you want SGOV included in position-alpha (so it shows as a row
with ~0 alpha against SPY for its dollar amount), remove it from
`_CASH_EQUIV_TICKERS` in `services/position_alpha.py`.

---

## 4. Verification — what to expect

A sanity check on the live data, run with the latest broker refresh:

```bash
# 1Y window, full portfolio
curl 'http://localhost:8000/api/portfolio/position-alpha?start_date=2025-05-18&end_date=2026-05-18'
# expect: total_alpha around +$9k, total_alpha_vs_qqq negative, total_alpha_vs_policy slightly positive

# 1Y window, active picks only
curl 'http://localhost:8000/api/portfolio/position-alpha?start_date=2025-05-18&end_date=2026-05-18&exclude_broad_index=true'
# expect: smaller V_start (~$167k), about same alpha sign
```

## 5. Legacy methodology (Modified Dietz)

Kept in code (`compute_performance_series` in
`services/performance.py`) but no longer rendered on the dashboard.
The matched-flow benchmark synthetic is still computed for the Risk
Metrics card, which is why F6 is still relevant.

Modified Dietz formula:
```
R(d) = (V(d) − V_start − Σ_{i: d_i ≤ d} C_i)
       / (V_start + Σ_{i: d_i ≤ d} C_i × (d − d_i) / (d − d_0))
```

Critical weakness: the denominator is contribution-weighted, which
makes the return % very sensitive to cashflow magnitudes. For a 1Y
window with $79k of contributions on a $537k base, the denominator
inflates ~$30-40k worth, dragging return percentages by several pp
even when actual dollar performance is small. The user spent several
sessions confused by this — leading to the position-alpha rewrite.

---

## 6. Glossary

| Term | Meaning |
|---|---|
| **V_start** | Aggregate dollar value of all positions on window-start date; cash is excluded |
| **V_end** | Same for window-end date |
| **Position alpha** | (V_end + sold − bought) − (V_end_spy + sold − bought) = V_end − V_end_spy |
| **Counterfactual** | "If your money had been in SPY/QQQ/Policy instead, applying the same per-trade $ flows" |
| **Walk-back** | Reverse-chronological reconstruction of historical quantities (not values) from the latest broker snapshot |
| **Anchor** | The earliest broker-verified `holdings_snapshots` row (currently 2026-05-09) |
