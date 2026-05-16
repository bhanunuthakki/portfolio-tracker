import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { Card, fmtUSD } from "@/components/ui";
import type { EarningsRow } from "@/types";

/**
 * Earnings calendar in month-grid view, one month at a time.
 *
 * Fetches the next 365 days of earnings (only_held=true), then renders
 * a 7-column month grid with all earnings on each calendar day. Prev/
 * next-month arrows navigate without refetching — the data set is
 * tiny relative to the query window.
 *
 * Color coding by proximity to today is preserved from the dashboard
 * card: red ≤7 days out, amber 8-14 days out, slate otherwise.
 */

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

function isoToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function startOfMonth(year: number, monthIdx: number): Date {
  return new Date(Date.UTC(year, monthIdx, 1));
}

function daysInMonth(year: number, monthIdx: number): number {
  return new Date(Date.UTC(year, monthIdx + 1, 0)).getUTCDate();
}

export function Earnings(): JSX.Element {
  const todayIso = isoToday();
  const today = new Date(todayIso + "T00:00:00Z");
  const [cursorYear, setCursorYear] = useState<number>(today.getUTCFullYear());
  const [cursorMonth, setCursorMonth] = useState<number>(today.getUTCMonth());

  const { data, isLoading, isError } = useQuery({
    queryKey: ["earnings-upcoming-year"],
    queryFn: () => api.upcomingEarnings({ daysAhead: 365, onlyHeld: true }),
  });

  // Bucket the rows by ISO date string for fast per-cell lookup.
  const rowsByDate = useMemo<Record<string, EarningsRow[]>>(() => {
    const out: Record<string, EarningsRow[]> = {};
    (data ?? []).forEach((r) => {
      const k = r.earnings_date;
      if (!out[k]) out[k] = [];
      out[k].push(r);
    });
    return out;
  }, [data]);

  const monthCells = useMemo(() => buildMonthCells(cursorYear, cursorMonth), [
    cursorYear,
    cursorMonth,
  ]);

  function shiftMonth(delta: number) {
    let m = cursorMonth + delta;
    let y = cursorYear;
    while (m < 0) {
      m += 12;
      y -= 1;
    }
    while (m > 11) {
      m -= 12;
      y += 1;
    }
    setCursorYear(y);
    setCursorMonth(m);
  }

  function goToToday() {
    setCursorYear(today.getUTCFullYear());
    setCursorMonth(today.getUTCMonth());
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-slate-900">
          Earnings calendar
        </h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Upcoming earnings dates for tickers in your latest holdings
          snapshot. Red ≤7 days, amber 8–14 days. Refreshed daily from
          yfinance.
        </p>
      </header>

      <Card>
        <header className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => shiftMonth(-1)}
              className="rounded p-1 text-slate-600 hover:bg-slate-100"
              aria-label="Previous month"
            >
              ←
            </button>
            <h2 className="min-w-[180px] text-center text-sm font-semibold text-slate-900">
              {MONTH_NAMES[cursorMonth]} {cursorYear}
            </h2>
            <button
              type="button"
              onClick={() => shiftMonth(1)}
              className="rounded p-1 text-slate-600 hover:bg-slate-100"
              aria-label="Next month"
            >
              →
            </button>
          </div>
          <button
            type="button"
            onClick={goToToday}
            className="rounded px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-100"
          >
            Today
          </button>
        </header>

        {isLoading ? (
          <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
        ) : isError ? (
          <div className="px-4 py-6 text-xs text-red-700">
            Failed to load earnings.
          </div>
        ) : (
          <>
            <div className="grid grid-cols-7 border-b border-slate-100 bg-slate-50 text-[11px] font-medium uppercase tracking-wide text-slate-500">
              {WEEKDAY_LABELS.map((d) => (
                <div key={d} className="px-2 py-1.5 text-center">
                  {d}
                </div>
              ))}
            </div>
            <div className="grid grid-cols-7">
              {monthCells.map((cell, idx) => (
                <DayCell
                  key={idx}
                  cell={cell}
                  rows={cell ? rowsByDate[cell.iso] ?? [] : []}
                  todayIso={todayIso}
                />
              ))}
            </div>
            <p className="border-t border-slate-100 bg-slate-50 px-4 py-2 text-xs text-slate-500">
              {(data ?? []).length} upcoming earnings across the next 365 days
              for held tickers.
            </p>
          </>
        )}
      </Card>
    </div>
  );
}

interface MonthCell {
  date: Date;
  iso: string;
  inMonth: boolean;
}

function buildMonthCells(year: number, monthIdx: number): (MonthCell | null)[] {
  const first = startOfMonth(year, monthIdx);
  const firstWeekday = first.getUTCDay();
  const total = daysInMonth(year, monthIdx);
  const cells: (MonthCell | null)[] = [];
  // Leading blanks
  for (let i = 0; i < firstWeekday; i++) cells.push(null);
  for (let d = 1; d <= total; d++) {
    const dt = new Date(Date.UTC(year, monthIdx, d));
    cells.push({
      date: dt,
      iso: dt.toISOString().slice(0, 10),
      inMonth: true,
    });
  }
  // Trailing blanks to fill to a multiple of 7 (max 6 weeks)
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

function DayCell({
  cell,
  rows,
  todayIso,
}: {
  cell: MonthCell | null;
  rows: EarningsRow[];
  todayIso: string;
}): JSX.Element {
  if (!cell) {
    return <div className="min-h-[88px] border-b border-r border-slate-100 bg-slate-50/40" />;
  }
  const isToday = cell.iso === todayIso;
  const isPast = cell.iso < todayIso;
  const dayNum = cell.date.getUTCDate();
  return (
    <div
      className={`min-h-[88px] border-b border-r border-slate-100 px-1.5 py-1 ${
        isPast ? "bg-slate-50/40" : "bg-white"
      }`}
    >
      <div className="flex items-center justify-between">
        <span
          className={`text-[10px] font-medium ${
            isToday
              ? "rounded-full bg-slate-900 px-1.5 text-white"
              : isPast
                ? "text-slate-400"
                : "text-slate-600"
          }`}
        >
          {dayNum}
        </span>
        {rows.length > 0 ? (
          <span className="text-[9px] text-slate-400">{rows.length}</span>
        ) : null}
      </div>
      <ul className="mt-1 space-y-0.5">
        {rows.slice(0, 4).map((r) => (
          <EarningsChip key={`${r.ticker}-${r.earnings_date}`} row={r} />
        ))}
        {rows.length > 4 ? (
          <li className="text-[9px] text-slate-500">+{rows.length - 4} more</li>
        ) : null}
      </ul>
    </div>
  );
}

function EarningsChip({ row }: { row: EarningsRow }): JSX.Element {
  let color = "bg-slate-100 text-slate-700";
  if (row.days_until <= 7) color = "bg-rose-100 text-rose-800";
  else if (row.days_until <= 14) color = "bg-amber-100 text-amber-800";
  const heldValue = row.held_value !== null ? parseFloat(row.held_value) : null;
  const title = [
    row.ticker,
    row.name ?? "",
    row.earnings_time ? `(${row.earnings_time})` : "",
    heldValue !== null ? `Held: ${fmtUSD(heldValue)}` : "",
    row.eps_estimate !== null ? `EPS est ${row.eps_estimate}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <li
      className={`truncate rounded px-1 py-0.5 text-[10px] font-medium ${color}`}
      title={title}
    >
      {row.ticker}
      {row.earnings_time ? (
        <span className="ml-1 text-[8px] opacity-70 uppercase">
          {row.earnings_time === "before_market" ? "BMO" : row.earnings_time === "after_market" ? "AMC" : ""}
        </span>
      ) : null}
    </li>
  );
}
