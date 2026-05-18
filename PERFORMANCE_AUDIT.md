# Performance Audit — Methodology, Rules, Known Distortions

Authored: 2026-05-18. Reviews `services/performance.py` (1576 LOC), the
transaction classification heuristic, and the backfill engine used for
the dashboard V chart and Trade Analysis P&L.

This document is intentionally blunt about what the pipeline can and
can't tell you. The "Known faults" section is the part you should care
about — anything else is bookkeeping.

---

## 0. Top-line on H2 2025

After the override pass applied on 2026-05-18 (121 transaction
classifications, see §3 below):

| Window | Portfolio | SPY | Diff |
|---|---|---|---|
| 2025-07-01 → 2025-12-31 | +13.52% | +10.44% | **+3.08 pp** |

So H2 2025 currently shows portfolio **outperforming** SPY by +3.08 pp.
If the dashboard line read otherwise the last time you looked, the cause
was unclassified cashflows (the May 16 / June 2 ACH withdrawals, the
Dec 23 BrokerageLink transfer, dozens of SoFi DCA deposits) — all now
tagged.

The +3.08 pp is *probably right* — but you should still read §4 because
the historical V series is fully modeled (zero broker snapshots before
2026-05-09), and the model has known biases that affect old dates more
than new ones.

---

## 1. Daily portfolio value — three-tier priority

For each date `d` in the requested window:

1. **`holdings_snapshots`** — Plaid+SnapTrade pulls written by the
   daily refresh job. Sum `institution_value` across active accounts.
   Authoritative when it exists.
   - **Current state**: zero rows before 2026-05-09. Every dated value
     on the chart before that day is reconstructed, not observed.

2. **`portfolio_values_daily`** cache — written by the daily refresh.
   Only used when a snapshot doesn't exist for the date. Snapshot wins
   on overlap (a stale cache value never clobbers a real snapshot).

3. **Backfill from transaction walk-back** — see §2. Only used for
   dates before the earliest snapshot. Each date's value =
   `sum(reconstructed_qty × historical_price) + cash_adjustment[d]`.

```
service.py:_daily_portfolio_value     ~ entry point
service.py:_forward_values_from_snapshots
service.py:_cached_daily_values
service.py:_backfill_values_from_transactions
```

---

## 2. Backfill walk-back

Anchored at the **first holdings_snapshot date** (currently 2026-05-09).
Walks `investment_transactions` in reverse chronological order to that
anchor, maintaining two parallel state machines:

**Positions (per security)**
- BUY → reverse subtracts `|qty|`
- SELL → reverse adds `|qty|`
- TRANSFER → reverse subtracts signed `qty`
- `cash/external_asset_transfer_in/out`, `cash/optionassignment`,
  `cash/optionexpiration`, `cash/rei` → reverse subtracts signed `qty`

**Cash adjustment (scalar Δ vs anchor cash)**
- BUY → cash was higher before the buy, so `cash_adj[t-1] += |amount|`
- SELL → cash was lower before the sell, so `cash_adj[t-1] -= |amount|`
- FEE → cash was higher before, so `cash_adj[t-1] += |amount|`
- CASH internal credit (dividend / interest) → cash was lower before, so
  `cash_adj[t-1] -= |amount|`
- External flow (deposit/withdrawal/ACATS) → routes through
  `_signed_cashflow` (see §3) and `cash_adj += -signed_cashflow`
- Cash-equivalent fees (paired margin-sweep noise) → skipped (sum to 0
  in theory, leak in practice)
- Share-moving cash subtypes (ACATS, option exercise, REI) → 0 cash
  delta (position-side handles them; amount is $0 or paired)

```
service.py:_reverse_transaction_quantity
service.py:_reverse_transaction_cash_delta
```

**Valuation** of reconstructed quantities, in priority order:

1. Cash equivalents (`is_cash_equivalent=True`, USD/MMFs) → qty × $1
2. Derivatives (`type=derivative`) → ignored when no price (post-expiry
   value is $0; constant fallback would create fake steps at trade dates)
3. Securities with yfinance/stooq history → forward-fill close ≤ date
4. Last-resort fallback: most recent `holdings_snapshots.institution_price`
   ever observed for that security — used when no price history exists
   (illiquid foreign tickers, niche ETFs)

```
service.py:_value_quantities_with_prices
```

---

## 3. Transaction classification rules

The single function `_signed_cashflow(type, subtype, amount, override, name)`
returns a signed cashflow value into the portfolio. Precedence is strict:

### 3a. Explicit override (`transaction_overrides` table)

User-supplied — never overridden by anything else. Values: `external_in`
(returns `+|amount|`), `external_out` (`-|amount|`), `internal` (`0`).

Currently in DB: 77 `external_in` + 29 `external_out` + 15 `internal` =
121 rows total.

### 3b. Name-hint (only applies when `type ∈ {cash, transfer}`)

Substring match on `tx.name`:

| Pattern | Classification |
|---|---|
| "reinvestment" or "drip" | `internal` (DRIP — dividend already counted) |
| "dividend" or "interest payment" or "credit interest" | `internal` (income event) |
| "outgoing" or "withdrawal" | `external_out` |
| "incoming" or "deposit" | `external_in` |

**Important**: the name-hint is gated to cashflow-eligible types so a
SELL of "Vanguard *Dividend* Appreciation ETF" doesn't get reclassified
as internal.

### 3c. Subtype heuristic — final fallback

For `tx.type = transfer`:
| Subtype | Classification |
|---|---|
| assignment, exercise, merger, spin off, split, stock distribution | `internal` |
| anything else (incl. `transfer/transfer`) | external — direction from `-amount` (Plaid sign convention: negative amount = cash entering) |

For `tx.type = cash`:
| Subtype | Direction |
|---|---|
| deposit, contribution, rollover, wire, ach | `external_in` (always `+|amount|`) |
| withdrawal | `external_out` (always `-|amount|`) |
| transfer (the bare `cash/transfer`) | direction from `-amount` |
| (others) dividend, interest, fee, rei | not cashflow; returns 0 |

**Direction precedence inside the heuristic**: for unambiguous subtypes
(`deposit`, `contribution`, `withdrawal`) the **name determines
direction** and we use `abs(amount)`. Brokers report `amount` sign
inconsistently — some from the cash account's perspective, some from
the investor's. For ambiguous subtypes we trust Plaid's standard
(negative `amount` = inflow).

### 3d. The `internal` margin-fee carousel (SoFi)

SoFi emits paired `fee/interest` + `fee/miscellaneous fee` +
`fee/margin expense` rows on the USD position that net to zero but
clutter activity logs. The walk-back skips `fee` on cash-equivalent
securities specifically to avoid leakage. Not a classification per se,
but worth knowing.

### 3e. Share-side ACATS unmatched flow

Handled separately at `service.py:_share_transfer_external_cashflows`.
SnapTrade emits `cash/external_asset_transfer_in/out` with `amount=$0`
(the dollar value isn't supplied). The walk-back values these at the
close price on the transfer date. If the in-leg and out-leg of an ACATS
both land in our DB on the same `(date, ticker, qty)` they cancel
internally; anything net is treated as external cashflow (deposit when
net qty > 0, withdrawal when net qty < 0).

---

## 4. Return calculation — Modified Dietz with money-flow-matched benchmarks

Per-date cumulative return:
```
R(d) = (V(d) − V_start − Σ_{i: d_i ≤ d} C_i)
       / (V_start + Σ_{i: d_i ≤ d} C_i × (d − d_i) / (d − d_0))
```
where `C_i` is each external cashflow (positive = in, negative = out)
and the cashflow weight equals the fraction of the window the cashflow
has been "working." Contributions late in the window count for less
denominator. Early withdrawals count for more (we're returning on less
capital from then on).

```
service.py:_modified_dietz_series
```

**Benchmark lines (SPY, QQQ, policy basket)** are constructed by
applying the **same daily external cashflow series** to a synthetic
portfolio that holds only the benchmark. So the matched-flow line
literally answers: "what would your money have done if every deposit /
withdrawal had been placed into SPY at that day's close?" This means
even when the reconstructed `V_start` is wrong, the comparison line
uses the same wrong base, so the *gap* between portfolio and benchmark
is more trustworthy than either absolute line.

```
service.py:_money_flow_matched_value
service.py:_policy_matched_value
```

---

## 5. Trade Analysis P&L (per-ticker)

Live formula (`services/trade_analysis.py:~265`):

```python
share_count_gap = (bought_qty + tolerance) < today_qty + sold_qty
                  OR sold_qty > bought_qty × (1 + tolerance)

if not share_count_gap:
    # All shares accounted for by buys → cashflow math is exact
    pnl = mv + sold - bought
else:
    if effective_cost_basis_known:
        pnl = mv − effective_cost   # Holdings-style
    else:
        pnl = mv + sold − bought    # fall through, flag unreliable
```

The flip from "always use cost basis when known" to "prefer cashflow
when shares are accounted for" was the fix on 2026-05-18 that took
NOW from +$36,540 to -$7,430 (the broker had reported $327 cost basis
on a ~$65k position).

A ticker is marked `cost_basis_unreliable` when either:
- Share-count math implies missing pre-history buys AND no override basis, OR
- At least one contributing account has a broker-reported cost basis
  under 5% of its institution_value (mirror of Holdings UNREL flag).

---

## 6. Known faults (intellectually honest section)

These are **real** sources of incorrect numbers. Ordered roughly by
how much they distort the chart.

### F1 — Zero broker snapshots before 2026-05-09 [SEVERE for old dates]

The entire historical chart (everything before May 9, 2026) is
reconstructed via walk-back. There is no broker-verified V at any
point in 2024 or 2025. The chart you're staring at for H2 2025 is
*entirely* a model.

**Mitigation in code**: `backfill_start_unreliable` flag fires when
reconstructed `V_start < 0.25 × V_end`, surfacing a BackfillWarning
banner. But this only triggers in extreme cases; modest distortions
slip through silently.

**What to do**: take a real broker snapshot daily going forward (the
cron is wired up; verify it's running). The pre-May-2026 history will
never improve unless you ingest 1099-Bs or back-snapshot via SnapTrade's
historical endpoint.

### F2 — Pre-history sells on 36 tickers [MATERIAL for those tickers]

You sold positions during the 2-year transaction window that were
acquired *before* the window. Plaid's 24-month retention means we have
the sells but not the matching buys. Worst offenders by share count:

| Ticker | Sold in window | Bought in window | Pre-history sh |
|---|---|---|---|
| JPM | 126.78 | 1.01 | 125.77 |
| BABA | 125.84 | 1 | 124.84 |
| OMF | 141.91 | 20 | 121.91 |
| TRMD | 556.80 | 451.83 | 104.98 |
| JD | 101 | 1 | 100 |
| TWLO | 101 | 1 | 100 |
| AMZN | 158.04 | 76.04 | 82 |
| VALE | 455.95 | 378.97 | 76.98 |
| VEU | 66.95 | 0 | 66.95 |
| FLIN | 217.15 | 168.07 | 49.08 |
| KGSPY | 41 | 0 | 41 |
| GOOG | 134.88 | 102.33 | 32.55 |
| MHO | 32.50 | 0 | 32.50 |

**Effect on backfill V**: walk-back adds these shares as we reverse
the sells, but has nothing to subtract back. So the early-window
reconstructed position is correct (we know they were held — they got
sold), but the *cash adjustment* picks up phantom inflows at the sales
that didn't actually fund anything else (the user already had that
basis). Modified Dietz then sees apparent "outperformance" attributable
to the sales when really it's pre-history gains being realized.

**Trade Analysis impact**: the `share_count_gap` detector fires for
these and falls back to cost-basis math. With no override, cost basis
defaults to $0 → P&L = today_value + sold − 0 → wildly overstated.
The `cost_basis_unreliable` flag fires for these (marked ⚠ in the UI),
but the dollar number is what you see and it's wrong.

**What to do**: backfill cost-basis overrides from 1099-Bs for these 36
tickers, OR write them off as "lifetime cashflow P&L" only and accept
that the windowed-return percentage attribution is approximate.

### F3 — `snapshot_price` fallback uses TODAY's price for historical valuation [MILD]

When a security has no yfinance/stooq price history (illiquid foreign
stock, niche ETF, weird Plaid ticker), the backfill falls back to the
most recent `holdings_snapshots.institution_price`. This is **today's**
price, not the historical price.

**Effect**: those securities show a flat line at today's price across
the entire backfilled window, missing all real intra-period movement.
If the user owned a position from 2024-06 that gained 3x by 2025-12,
the chart shows it at the 3x price across the whole period —
overstating early V.

**Mitigation**: skipped for derivatives (where snapshot is $0 at
expiry). For non-derivatives, we accept the bias.

### F4 — `active_account_ids` filter drops history of disabled items [MILD–MODERATE]

`active_account_ids(session)` returns accounts where
`items.is_data_active = 1`. Disabled items' transactions and snapshots
are excluded from EVERY query. If an item was active during, say, H1
2025 but you disabled it later, all its historical transactions
disappear from the chart **as if they never happened**.

**Effect**: phantom dips when an account's history vanishes between
chart loads. Net contributions can also flip sign retroactively.

**What to do**: only disable an item if you're certain it should be
excluded from history forever (e.g., spouse's account, account
duplicated by two aggregators). Otherwise leave items enabled even
after the broker connection breaks.

### F5 — Broker sign convention disagreement [LARGELY MITIGATED]

Plaid signs `amount` from the cash account's perspective (buy positive,
sell negative). SnapTrade signs it the opposite way (buy negative, sell
positive). The walk-back uses `|amount|` + transaction type to pick the
direction — *not* the raw sign. This avoids phantom spikes on
cross-source trades but means any code path that uses raw `amount`
without going through `_reverse_transaction_cash_delta` /
`_signed_cashflow` is buggy.

**Audit needed**: grep for any `tx.amount` use outside those helpers.

### F6 — DRIP rows tagged `internal` instead of cashflow-neutral [HARMLESS but worth knowing]

Dividend reinvestment shows up as a `transfer/transfer` with name
"Dividend reinvestment purchase of N shares." The DIVIDEND itself
already entered V as a `cash/dividend` row (counted as internal income
that increases V via cash). The REI then BUYS shares using that same
cash. So the REI line should be cashflow-neutral.

We classify REI as `internal` via the name-hint, returning 0
cashflow. Correct outcome.

### F7 — Cost-basis-derived unrealized P&L still uses broker number on partially-clean tickers [SUBTLE]

For tickers with both reliable override accounts AND broken broker
accounts (e.g., BN: SoFi 708 sh at $41,276 override + BrokerageLink 273
sh at junk $48 broker cost), the combined `effective_cost` mixes good
and bad numbers. The new heuristic flags the ticker `cost_basis_unreliable`
but still computes P&L using the contaminated sum.

**Effect on trade-analysis**: P&L is a mix of legit (override portion) and
overstated (broker portion). Marked ⚠ but dollar number is wrong.

**What to do**: set per-account manual overrides for the remaining
UNREL broker accounts. The Holdings drill-down editor (committed
2026-05-18) is wired for this.

### F8 — Modified Dietz denominator uses reconstructed `V_start` [SEVERE for pre-2026-05-09 windows]

When the user picks a chart window starting before 2026-05-09, the
denominator `V_start + weighted_cashflows` uses the BACKFILLED start
value. If that backfill is wrong by $50k, every cumulative return %
on the chart for that window inherits the bias.

**Matched-flow benchmarks help**: SPY-equivalent and QQQ-equivalent
use the same wrong `V_start`, so their lines drift in lockstep. The
**gap** between portfolio and benchmark is more reliable than either
absolute number.

### F9 — Lifetime cumulative return reads +13.09% but with a 121-row override applied 12 hours ago [CONTEXTUAL]

You just applied a large override pass. Any chart pulled before today's
override application would show different numbers. If you saved a
screenshot from before the override pass, it's stale.

---

## 7. Specific recommendations

1. **Verify the daily-refresh cron is writing snapshots tonight**. The
   absence of snapshots between today and historic dates means even
   "yesterday" gets reconstructed. Check `scripts/logs/daily_refresh_*.log`.

2. **Set manual cost-basis overrides** for the 13 remaining UNREL
   accounts on Holdings (mostly NOW, NVO, BN, WIX). The inline editor
   was wired up today.

3. **Don't trust per-ticker P&L** for the 36 pre-history tickers.
   The ⚠ marker is in the UI; treat any displayed dollar P&L for those
   names as "broker-derived and incomplete."

4. **Use the end-date anchor** (added today) to view the chart "as of"
   key dates. Comparing portfolio vs SPY across the same window is the
   strongest signal — the matched-flow benchmark cancels out the
   `V_start` reconstruction bias.

5. **Refresh broker data before any analysis** — `python -m
   portfolio_tracker.jobs.daily_refresh` — to lock in tonight's
   snapshot as new ground truth. Every additional day of real
   snapshots reduces the modeled fraction of history.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| V(d) | Portfolio value on date d (sum across active accounts) |
| Anchor | The earliest broker-verified `holdings_snapshots` row (currently 2026-05-09) |
| Walk-back | The reverse-chronological transaction replay used to reconstruct V before the anchor |
| Modified Dietz | Time-weighted return approximation where each cashflow is weighted by its fraction of the window |
| Matched-flow benchmark | A synthetic SPY/QQQ/policy portfolio that experiences your exact daily cashflow series at the benchmark's close prices |
| External cashflow | Money in or out of the tracked portfolio (deposits, withdrawals, ACATS to untracked accounts) — affects V but not returns |
| Internal flow | Money movements that stay within tracked accounts (dividends, fees, DRIPs, between-account transfers where both ends are tracked) — affect V via market action only |
| `cost_basis_unreliable` | Heuristic flag: broker-reported cost basis < 5% of market value on a ≥$1k position, OR walk-back implies missing pre-history buys |
