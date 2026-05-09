import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { Card, fmtUSD } from "@/components/ui";
import type { EarningsRow } from "@/types";

/**
 * Upcoming earnings for currently-held positions.
 *
 * Uses `/api/earnings/upcoming?only_held=true` so the surface only
 * shows names you're actually exposed to. Rows are color-coded by
 * proximity (red ≤7d, amber ≤14d) so the next thing on the calendar
 * is impossible to miss. Held value is shown so you can prioritize
 * — a $50k position with earnings tomorrow matters more than a $2k
 * position with earnings next month.
 *
 * Refresh cadence: the daily-refresh cron pulls from yfinance once a
 * day. If the table is empty when this card mounts, it just renders
 * an empty state — no inline-fetch on render to keep the page snappy.
 */
export function EarningsCalendarCard(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["earnings-upcoming"],
    queryFn: () => api.upcomingEarnings({ daysAhead: 90, onlyHeld: true }),
  });

  return (
    <Card>
      <header className="border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">
          Earnings calendar — held positions
        </h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Upcoming earnings dates for tickers in your latest holdings
          snapshot. Refreshed daily from yfinance.
        </p>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load earnings.
        </div>
      ) : !data || data.length === 0 ? (
        <div className="px-4 py-6 text-xs text-slate-500">
          No upcoming earnings in the next 90 days for held tickers. (ETFs
          and cash equivalents don&apos;t report earnings — that&apos;s
          expected.)
        </div>
      ) : (
        <ul className="divide-y divide-slate-100">
          {data.map((row) => (
            <EarningsRowCard key={`${row.ticker}-${row.earnings_date}`} row={row} />
          ))}
        </ul>
      )}
    </Card>
  );
}

function EarningsRowCard({ row }: { row: EarningsRow }): JSX.Element {
  // Tighter band → more attention. ≤7 days = "this week, prep now."
  const proximityClass =
    row.days_until <= 7
      ? "border-l-4 border-red-500 bg-red-50/30"
      : row.days_until <= 14
        ? "border-l-4 border-amber-500 bg-amber-50/30"
        : "border-l-4 border-slate-200";

  const epsEst = row.eps_estimate !== null ? parseFloat(row.eps_estimate) : null;
  const epsAct = row.eps_actual !== null ? parseFloat(row.eps_actual) : null;

  return (
    <li className={`${proximityClass} px-4 py-2.5`}>
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2.5 min-w-0">
          <span className="font-mono font-semibold text-slate-900">
            {row.ticker}
          </span>
          <span className="text-xs text-slate-500 truncate">{row.name}</span>
        </div>
        <div className="text-xs tabular-nums text-slate-500 shrink-0">
          {row.earnings_date} · in {row.days_until}d
        </div>
      </div>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-slate-600">
        {epsEst !== null && (
          <span>
            <span className="font-medium text-slate-700">EPS est:</span>{" "}
            <span className="tabular-nums">{epsEst.toFixed(2)}</span>
          </span>
        )}
        {epsAct !== null && (
          <span>
            <span className="font-medium text-slate-700">EPS actual:</span>{" "}
            <span className="tabular-nums">{epsAct.toFixed(2)}</span>
          </span>
        )}
        {row.held_value && parseFloat(row.held_value) > 0 && (
          <span>
            <span className="font-medium text-slate-700">Held:</span>{" "}
            <span className="tabular-nums">
              {fmtUSD(parseFloat(row.held_value))}
            </span>
          </span>
        )}
      </div>
    </li>
  );
}
