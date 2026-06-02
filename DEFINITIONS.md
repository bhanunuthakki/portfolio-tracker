# Definitions

Canonical domain vocabulary. Use these terms verbatim in code (variables,
functions, types, columns), comments, commit messages, and conversation.
Seeded with the **positioning** vocabulary; extend as other areas are
standardized.

## Positioning

- **Positioning** — the breakdown of the *current* book (latest snapshot of
  active accounts) along several axes: asset type, sector, region, account
  tax-treatment, concentration, and per-ticker correlation. Surfaced as a
  section on the Holdings page. Always value-weighted by current market value.

- **Asset type** — coarse instrument class of a security, derived from the
  broker `type` code plus the cash-equivalent flag. Canonical values:
  `Stock`, `ETF`, `Mutual fund`, `Crypto`, `Cash`, `Other`. ADRs are `Stock`.
  Money-market funds (flagged cash-equivalent) are `Cash`.

- **Sector** — GICS-style sector for an individual equity (e.g. `Technology`).
  Funds have no single sector and are bucketed as **`ETF/Fund`**; crypto as
  `Crypto`; cash as `Cash`; an unclassified equity as `Unclassified`.

- **Region** — coarse domicile cut. Canonical values: `US`, `International`,
  `Unknown`. Derived from the classified country (`United States` ⇒ `US`).

- **Tax treatment** — an account's tax location. Canonical values: `Taxable`,
  `Retirement / tax-advantaged` (IRA / 401(k) / Roth / HSA / 529 / …),
  `Unknown`. Derived from account subtype, with a name fallback for feeds that
  omit it.

- **Classification (security)** — the cached `(sector, region)` for a security,
  stored in `security_classifications`. **Source** is `auto` (yfinance
  enrichment) or `manual` (user override); the enrichment job never overwrites
  a `manual` row.

- **Concentration** — how concentrated the book is across consolidated-by-
  security positions: top-N weight (`top1`/`top5`/`top10`), position count,
  **HHI** (Herfindahl-Hirschman Index, Σ of squared percent weights, 0–10000),
  and **effective holdings** (`1 / Σ(weight_fraction²)` — "behaves like ~N
  equally-weighted positions").

- **Correlation / beta (per ticker)** — a security's daily-return correlation
  and beta to a benchmark (`SPY`, `QQQ`, or the policy mix) over the window,
  via the OLS routine in `services/beta.py`. Cash is excluded.
