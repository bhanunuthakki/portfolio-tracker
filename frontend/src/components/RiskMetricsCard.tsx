import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { Card } from "@/components/ui";

type Benchmark = "SPY" | "QQQ";

/**
 * Risk metrics relative to a chosen benchmark.
 *
 * Beta alone is a single number, but it lives in a context: alpha tells
 * you idiosyncratic return, R² tells you how seriously to take beta,
 * volatilities give scale, and the notes flag known limitations of the
 * sample. Putting them in their own card keeps the Dashboard stat row
 * focused on portfolio totals.
 */
export function RiskMetricsCard({
  startDate,
  endDate,
  includeBackfill,
}: {
  startDate?: string;
  endDate?: string;
  includeBackfill: boolean;
}): JSX.Element {
  const [benchmark, setBenchmark] = useState<Benchmark>("SPY");

  const { data, isLoading, isError } = useQuery({
    queryKey: ["beta", { startDate, endDate, includeBackfill, benchmark }],
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
          {(["SPY", "QQQ"] as const).map((b) => (
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
              {b}
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
          <div className="grid grid-cols-2 gap-x-6 gap-y-4 px-4 py-4 sm:grid-cols-3 lg:grid-cols-6">
            <Metric
              label="Beta"
              value={fmtNumber(data.beta, 2)}
              hint={
                data.beta !== null
                  ? data.beta > 1.05
                    ? `${((data.beta - 1) * 100).toFixed(0)}% more volatile than ${benchmark}`
                    : data.beta < 0.95
                      ? `${((1 - data.beta) * 100).toFixed(0)}% less volatile than ${benchmark}`
                      : `moves in lockstep with ${benchmark}`
                  : undefined
              }
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
              hint={
                data.r_squared !== null
                  ? data.r_squared > 0.7
                    ? "beta is a good summary"
                    : data.r_squared > 0.4
                      ? "beta is a partial summary"
                      : "beta is a weak summary"
                  : undefined
              }
            />
            <Metric
              label="Correlation"
              value={fmtNumber(data.correlation, 2)}
              hint="−1 to +1; 0 means uncorrelated"
            />
            <Metric
              label="Portfolio σ"
              value={fmtPctFromFraction(data.portfolio_volatility_annualized)}
              hint="annualized volatility"
            />
            <Metric
              label={`${benchmark} σ`}
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
            Beta is the slope of an OLS regression of portfolio daily returns
            against {benchmark} daily returns. R² indicates how much of your
            day-to-day variation that single line explains — low R² (typical
            for concentrated or international books) means a single beta
            number isn&apos;t the full story.
          </p>
        </>
      )}
    </Card>
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
      <div className="text-[11px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
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
