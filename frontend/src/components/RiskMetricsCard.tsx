import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { Card } from "@/components/ui";

type Benchmark = "SPY" | "QQQ" | "POLICY";

const BENCHMARK_LABELS: Record<Benchmark, string> = {
  SPY: "SPY",
  QQQ: "QQQ",
  POLICY: "Policy",
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
}: {
  startDate?: string;
  endDate?: string;
}): JSX.Element {
  const [benchmark, setBenchmark] = useState<Benchmark>("POLICY");

  const { data, isLoading, isError } = useQuery({
    queryKey: ["beta", { startDate, endDate, benchmark }],
    queryFn: () =>
      api.beta({
        startDate,
        endDate,
        benchmark,
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
            />
            <Metric
              label="R²"
              value={fmtNumber(data.r_squared, 2)}
              hint={rSquaredHint(data.r_squared)}
            />
            <Metric
              label="Correlation"
              value={fmtNumber(data.correlation, 2)}
              hint="−1 to +1; 0 means uncorrelated"
            />
            <Metric
              label="Information Ratio"
              value={fmtNumber(data.information_ratio, 2)}
              hint={irHint(data.information_ratio)}
              valueClass={pnlClassFromNumber(data.information_ratio)}
            />
            <Metric
              label="Tracking error"
              value={fmtPctFromFraction(data.tracking_error_annualized)}
              hint="annualized σ of return spread"
            />

            {/* Section: absolute risk-adjusted */}
            <SectionHeading>Absolute (rf = {(data.risk_free_annual * 100).toFixed(1)}%)</SectionHeading>
            <Metric
              label="Sharpe"
              value={fmtNumber(data.sharpe, 2)}
              hint="excess return / total σ"
              valueClass={pnlClassFromNumber(data.sharpe)}
            />
            <Metric
              label="Sortino"
              value={fmtNumber(data.sortino, 2)}
              hint="excess return / downside σ"
              valueClass={pnlClassFromNumber(data.sortino)}
            />
            <Metric label="" value="" hint="" />
            <Metric
              label="Portfolio σ"
              value={fmtPctFromFraction(data.portfolio_volatility_annualized)}
              hint="annualized volatility"
            />
            <Metric
              label={`${BENCHMARK_LABELS[benchmark]} σ`}
              value={fmtPctFromFraction(data.benchmark_volatility_annualized)}
              hint="annualized volatility"
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
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
}): JSX.Element {
  return (
    <div className="min-w-0">
      {label && (
        <div className="text-[11px] uppercase tracking-wider text-slate-500">
          {label}
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
