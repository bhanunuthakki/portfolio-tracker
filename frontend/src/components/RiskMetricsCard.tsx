import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "@/api/client";
import { Card } from "@/components/ui";

type Benchmark = "SPY" | "QQQ" | "POLICY";

const BENCHMARK_LABELS: Record<Benchmark, string> = {
  SPY: "SPY",
  QQQ: "QQQ",
  POLICY: "Policy",
};

interface MetricExplainer {
  definition: string;
  formula?: string;
  interpretation?: string;
}

/**
 * Definitions, formulas, and reading guides for each metric. Click the (?)
 * icon next to a metric label to surface the matching entry. Treat formulas
 * as the source of truth — `services/beta.py` implements them directly.
 */
const EXPLAINERS: Record<string, MetricExplainer> = {
  beta: {
    definition:
      "Sensitivity of portfolio returns to benchmark returns — the slope of an OLS regression of daily portfolio returns on daily benchmark returns.",
    formula: "β = Cov(Rₚ, R_b) / Var(R_b)",
    interpretation:
      "β = 1 moves 1:1 with the benchmark. β = 1.2 is 20% more volatile in the same direction. β < 0 means inverse co-movement. Only meaningful when R² is reasonably high.",
  },
  alpha: {
    definition:
      "Annualized excess return not explained by benchmark exposure — the intercept of the regression, scaled to a year.",
    formula: "α_daily = mean(Rₚ) − β × mean(R_b);  α_annual = α_daily × 252",
    interpretation:
      "Positive ⇒ outperforming the regression line; negative ⇒ underperforming. When R² is low, alpha is noisy and largely captures style drift, not skill.",
  },
  rSquared: {
    definition:
      "Fraction of portfolio variance explained by the benchmark — equivalently the squared correlation.",
    formula: "R² = ρ² = 1 − SS_residual / SS_total",
    interpretation:
      "0 to 1. > 0.7 ⇒ beta is a good summary. 0.4 – 0.7 ⇒ partial. < 0.4 ⇒ beta is a weak summary; consider a different benchmark or a multi-factor model.",
  },
  correlation: {
    definition:
      "Linear co-movement strength between portfolio and benchmark daily returns. Sign matches beta's sign.",
    formula: "ρ = Cov(Rₚ, R_b) / (σₚ · σ_b)",
    interpretation:
      "−1 to +1. 0 is uncorrelated, ±1 is a perfect line. Unlike beta, correlation is unitless and bounded.",
  },
  informationRatio: {
    definition:
      "Risk-adjusted excess return vs the benchmark — how consistently you beat it after accounting for tracking volatility.",
    formula: "IR = mean(Rₚ − R_b) / σ(Rₚ − R_b) × √252",
    interpretation:
      "> 0 ⇒ outperforming. > 0.5 is good, > 1.0 excellent. Negative means the benchmark is consistently ahead.",
  },
  trackingError: {
    definition:
      "Annualized volatility of the daily return spread between portfolio and benchmark.",
    formula: "TE = σ(Rₚ − R_b) × √252",
    interpretation:
      "Index funds typically run < 1%. Active funds run 4 – 8%. Higher TE means the portfolio's path diverges more from the benchmark's.",
  },
  sharpe: {
    definition:
      "Excess return over the risk-free rate per unit of total volatility. Absolute (no benchmark needed).",
    formula: "Sharpe = mean(Rₚ − r_f) / σₚ × √252",
    interpretation:
      "> 1 is good, > 2 is great. Penalizes upside and downside vol equally — can understate skewed-positive strategies.",
  },
  sortino: {
    definition:
      "Excess return per unit of downside volatility only. Doesn't penalize upside surprises.",
    formula:
      "Sortino = mean(Rₚ − r_f) / σ_down × √252,  where σ_down uses only days with Rₚ < r_f",
    interpretation:
      "Usually higher than Sharpe. Better for asymmetric strategies — concentrated equity, tail-hedged, options-overlay, etc.",
  },
  portfolioVol: {
    definition:
      "Annualized standard deviation of daily portfolio returns — total absolute risk.",
    formula: "σₚ × √252",
    interpretation:
      "Pure volatility, no benchmark. Compare against the benchmark σ below to see whether you're taking more or less total risk than the index.",
  },
  benchmarkVol: {
    definition: "Annualized standard deviation of daily benchmark returns.",
    formula: "σ_b × √252",
    interpretation:
      "Reference point for the portfolio σ above.",
  },
};

/**
 * Risk + risk-adjusted-return metrics relative to a chosen benchmark.
 *
 * Single-factor beta vs an index is a thin summary — for a portfolio with
 * concentrated bets, international exposure, or systematic de-risking,
 * R² is usually low and "alpha vs SPY" mostly captures style drift, not
 * skill. Comparing against the user's own POLICY mix (a synthetic
 * portfolio matching their target allocation) is more honest.
 *
 * Sharpe / Sortino are absolute (no benchmark needed) — risk-adjusted
 * return per unit of total / downside volatility. Information Ratio is
 * the consistency of beating the benchmark.
 */
export function RiskMetricsCard({
  startDate,
  endDate,
  excludeIndexEtfs = false,
  reserveAmount = 0,
}: {
  startDate?: string;
  endDate?: string;
  excludeIndexEtfs?: boolean;
  reserveAmount?: number;
}): JSX.Element {
  const [benchmark, setBenchmark] = useState<Benchmark>("POLICY");

  const { data, isLoading, isError } = useQuery({
    queryKey: [
      "beta",
      { startDate, endDate, benchmark, excludeIndexEtfs, reserveAmount },
    ],
    queryFn: () =>
      api.beta({
        startDate,
        endDate,
        benchmark,
        excludeIndexEtfs,
        reserveAmount,
      }),
  });

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-sm font-semibold text-slate-900">
          Risk metrics vs benchmark
        </h2>
        <div className="flex items-center gap-1">
          {(["POLICY", "SPY", "QQQ"] as const).map((b) => (
            <button
              key={b}
              type="button"
              onClick={() => setBenchmark(b)}
              className={[
                "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                benchmark === b
                  ? "bg-slate-900 text-white"
                  : "text-slate-600 hover:bg-slate-100",
              ].join(" ")}
            >
              {BENCHMARK_LABELS[b]}
            </button>
          ))}
        </div>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError || !data ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load risk metrics.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-x-6 gap-y-4 px-4 py-4 sm:grid-cols-3 lg:grid-cols-3">
            {/* Section: vs benchmark (regression) */}
            <SectionHeading>vs {BENCHMARK_LABELS[benchmark]}</SectionHeading>
            <Metric
              label="Beta"
              value={fmtNumber(data.beta, 2)}
              hint={betaHint(data.beta, benchmark)}
              explainer={EXPLAINERS.beta}
            />
            <Metric
              label="Alpha (annualized)"
              value={
                data.alpha_annualized_pct !== null
                  ? `${data.alpha_annualized_pct >= 0 ? "+" : ""}${data.alpha_annualized_pct.toFixed(1)}%`
                  : "—"
              }
              hint="excess return not explained by beta"
              valueClass={pnlClassFromNumber(data.alpha_annualized_pct)}
              explainer={EXPLAINERS.alpha}
            />
            <Metric
              label="R²"
              value={fmtNumber(data.r_squared, 2)}
              hint={rSquaredHint(data.r_squared)}
              explainer={EXPLAINERS.rSquared}
            />
            <Metric
              label="Correlation"
              value={fmtNumber(data.correlation, 2)}
              hint="−1 to +1; 0 means uncorrelated"
              explainer={EXPLAINERS.correlation}
            />
            <Metric
              label="Information Ratio"
              value={fmtNumber(data.information_ratio, 2)}
              hint={irHint(data.information_ratio)}
              valueClass={pnlClassFromNumber(data.information_ratio)}
              explainer={EXPLAINERS.informationRatio}
            />
            <Metric
              label="Tracking error"
              value={fmtPctFromFraction(data.tracking_error_annualized)}
              hint="annualized σ of return spread"
              explainer={EXPLAINERS.trackingError}
            />

            {/* Section: absolute risk-adjusted */}
            <SectionHeading>Absolute (rf = {(data.risk_free_annual * 100).toFixed(1)}%)</SectionHeading>
            <Metric
              label="Sharpe"
              value={fmtNumber(data.sharpe, 2)}
              hint="excess return / total σ"
              valueClass={pnlClassFromNumber(data.sharpe)}
              explainer={EXPLAINERS.sharpe}
            />
            <Metric
              label="Sortino"
              value={fmtNumber(data.sortino, 2)}
              hint="excess return / downside σ"
              valueClass={pnlClassFromNumber(data.sortino)}
              explainer={EXPLAINERS.sortino}
            />
            <Metric label="" value="" hint="" />
            <Metric
              label="Portfolio σ"
              value={fmtPctFromFraction(data.portfolio_volatility_annualized)}
              hint="annualized volatility"
              explainer={EXPLAINERS.portfolioVol}
            />
            <Metric
              label={`${BENCHMARK_LABELS[benchmark]} σ`}
              value={fmtPctFromFraction(data.benchmark_volatility_annualized)}
              hint="annualized volatility"
              explainer={EXPLAINERS.benchmarkVol}
            />
          </div>

          <div className="border-t border-slate-100 px-4 py-2.5 text-xs text-slate-500">
            Sample: {data.sample_size} paired daily observations (
            <span className="tabular-nums">
              {data.start_date} → {data.end_date}
            </span>
            )
          </div>

          {data.notes.length > 0 && (
            <ul className="border-t border-slate-100 bg-amber-50 px-4 py-3 text-xs text-amber-900 space-y-1">
              {data.notes.map((n, idx) => (
                <li key={idx}>· {n}</li>
              ))}
            </ul>
          )}

          <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
            Beta + alpha + R² come from an OLS regression of portfolio daily
            returns on the benchmark&apos;s. Sharpe / Sortino are absolute
            risk-adjusted returns vs cash. Information Ratio is consistency
            of outperformance — &gt; 0.5 is good, &gt; 1.0 is excellent.
            Edit policy weights on the Accounts page to change the Policy
            benchmark.
          </p>
        </>
      )}
    </Card>
  );
}

function SectionHeading({ children }: { children: string }): JSX.Element {
  return (
    <div className="col-span-full pt-1 text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-100 pb-1.5">
      {children}
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  valueClass,
  explainer,
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
  explainer?: MetricExplainer;
}): JSX.Element {
  return (
    <div className="min-w-0">
      {label && (
        <div className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-slate-500">
          <span>{label}</span>
          {explainer && <MetricInfoButton label={label} explainer={explainer} />}
        </div>
      )}
      <div
        className={[
          "mt-1 text-lg font-semibold tabular-nums text-slate-900 truncate",
          valueClass ?? "",
        ].join(" ")}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

/**
 * Click-toggle popover with the metric's definition, formula, and reading
 * guide. Closes on outside click or Escape.
 *
 * Implementation notes: native <button> handles Enter/Space activation; the
 * only manually wired keyboard behavior is Escape-to-close, which isn't
 * focus-management — it's a single global listener while open. Hover is
 * deliberately NOT used to toggle: formulas and interpretation paragraphs
 * are too long for transient tooltips, and hover is inaccessible on touch.
 */
function MetricInfoButton({
  label,
  explainer,
}: {
  label: string;
  explainer: MetricExplainer;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative inline-flex">
      <button
        type="button"
        aria-label={`What is ${label}?`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-slate-300 text-[9px] font-bold normal-case text-slate-500 hover:border-slate-500 hover:text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-slate-700"
      >
        ?
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={`${label} explainer`}
          className="absolute left-0 top-full z-20 mt-1.5 w-72 max-w-[calc(100vw-2rem)] rounded-md border border-slate-200 bg-white p-3 text-left text-xs normal-case tracking-normal text-slate-700 shadow-lg"
        >
          <div className="text-sm font-semibold text-slate-900">{label}</div>
          <p className="mt-1.5 leading-relaxed">{explainer.definition}</p>
          {explainer.formula && (
            <div className="mt-2.5">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                Formula
              </div>
              <div className="mt-1 rounded bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-800">
                {explainer.formula}
              </div>
            </div>
          )}
          {explainer.interpretation && (
            <div className="mt-2.5">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                Reading it
              </div>
              <p className="mt-1 leading-relaxed">{explainer.interpretation}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function fmtNumber(value: number | null, fractionDigits: number): string {
  if (value === null) return "—";
  return value.toFixed(fractionDigits);
}

function fmtPctFromFraction(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function pnlClassFromNumber(value: number | null): string {
  if (value === null) return "";
  if (value > 0) return "text-emerald-700";
  if (value < 0) return "text-red-700";
  return "";
}

function betaHint(beta: number | null, benchmark: Benchmark): string | undefined {
  if (beta === null) return undefined;
  const label = BENCHMARK_LABELS[benchmark];
  if (beta > 1.05) return `${((beta - 1) * 100).toFixed(0)}% more volatile than ${label}`;
  if (beta < 0.95) return `${((1 - beta) * 100).toFixed(0)}% less volatile than ${label}`;
  return `moves in lockstep with ${label}`;
}

function rSquaredHint(r2: number | null): string | undefined {
  if (r2 === null) return undefined;
  if (r2 > 0.7) return "beta is a good summary";
  if (r2 > 0.4) return "beta is a partial summary";
  return "beta is a weak summary";
}

function irHint(ir: number | null): string | undefined {
  if (ir === null) return undefined;
  if (ir > 1) return "excellent";
  if (ir > 0.5) return "good";
  if (ir > 0) return "weak positive";
  return "underperforming benchmark";
}
