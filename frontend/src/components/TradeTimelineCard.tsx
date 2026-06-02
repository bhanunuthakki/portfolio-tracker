import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { Card, SortableTh, Td, fmtSignedUSD, fmtUSD, pnlClass } from "@/components/ui";
import type { SortControl } from "@/components/ui";
import { useTableSort } from "@/components/useTableSort";
import type { TimelineRow, YearSummary } from "@/types";

/**
 * Chronological trade timeline with SPY counterfactual.
 *
 * For each closed lot (1099 data) and each currently-held position (broker
 * snapshot), we compare the realized/unrealized gain to what SPY would have
 * returned over the same holding window. Alpha = excess vs SPY.
 *
 * Every column is click-sortable (asc/desc) via useTableSort + SortableTh.
 * The chips above the table are quick presets that jump the shared sort
 * state to a common view: Recent (disposed_date desc), Best alpha (alpha
 * desc), Worst alpha (alpha asc).
 */

const PAGE_SIZE = 25;

export function TradeTimelineCard(): JSX.Element {
  const [year, setYear] = useState<number | "all">("all");
  const [showAll, setShowAll] = useState<boolean>(false);

  // Fetch the full timeline once; year filter is client-side so the chip
  // bar stays stable as the user navigates between years.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["trade-timeline"],
    queryFn: () => api.tradeTimeline({ includeOpen: true }),
  });

  const filteredRows = useMemo<TimelineRow[]>(() => {
    if (!data) return [];
    return data.rows.filter((r) => {
      if (year === "all") return true;
      // Open positions only show when "All" is selected — they don't belong
      // to any single tax year.
      if (r.row_kind === "open") return false;
      return r.tax_year === year;
    });
  }, [data, year]);

  const accessors = useMemo(
    () => ({
      disposed_date: (r: TimelineRow) => r.disposed_date,
      ticker: (r: TimelineRow) => r.ticker ?? "",
      source: (r: TimelineRow) => formatSource(r),
      held: (r: TimelineRow) => r.holding_days,
      cost: (r: TimelineRow) => parseFloat(r.cost_basis),
      pnl: (r: TimelineRow) => parseFloat(r.realized_gain),
      return: (r: TimelineRow) => r.return_pct,
      spy: (r: TimelineRow) =>
        r.spy_counterfactual_dollars !== null
          ? parseFloat(r.spy_counterfactual_dollars)
          : null,
      alpha: (r: TimelineRow) =>
        r.alpha_dollars !== null ? parseFloat(r.alpha_dollars) : null,
    }),
    [],
  );
  // Default: most recently disposed first ("what did I do lately").
  const sort = useTableSort(filteredRows, "disposed_date", "desc", accessors);
  const sortedRows = sort.sortedRows;

  const visibleRows = showAll ? sortedRows : sortedRows.slice(0, PAGE_SIZE);

  const yearOptions = useMemo<number[]>(() => {
    const years = new Set<number>();
    data?.by_year.forEach((y) => years.add(y.year));
    return [...years].sort((a, b) => b - a);
  }, [data]);

  const summaryForYear = useMemo<YearSummary | null>(() => {
    if (!data) return null;
    if (year === "all") {
      // Aggregate across all years
      const agg: YearSummary = {
        year: 0,
        closed_count: 0,
        realized_total: "0",
        spy_counterfactual_total: "0",
        alpha_total: "0",
      };
      let realized = 0,
        spy = 0,
        alpha = 0;
      data.by_year.forEach((y) => {
        agg.closed_count += y.closed_count;
        realized += parseFloat(y.realized_total);
        spy += parseFloat(y.spy_counterfactual_total);
        alpha += parseFloat(y.alpha_total);
      });
      agg.realized_total = String(realized);
      agg.spy_counterfactual_total = String(spy);
      agg.alpha_total = String(alpha);
      return agg;
    }
    return data.by_year.find((y) => y.year === year) ?? null;
  }, [data, year]);

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Trade timeline vs SPY
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Chronological view of every closed lot (1099-B) plus open positions,
            scored against a buy-and-hold-SPY counterfactual over the same
            holding window. Alpha = your gain − what SPY would&apos;ve done.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <YearChip
            label="All"
            active={year === "all"}
            onClick={() => setYear("all")}
          />
          {yearOptions.map((y) => (
            <YearChip
              key={y}
              label={String(y)}
              active={year === y}
              onClick={() => setYear(y)}
            />
          ))}
        </div>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : isError || !data ? (
        <div className="px-4 py-6 text-xs text-red-700">
          Failed to load trade timeline.
        </div>
      ) : (
        <>
          <SummaryStrip summary={summaryForYear} totalRows={sortedRows.length} />

          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2 text-xs">
            <div className="flex items-center gap-1">
              <span className="mr-1 text-slate-400">Jump to:</span>
              <SortChip
                label="Recent"
                active={
                  sort.sortKey === "disposed_date" && sort.sortDir === "desc"
                }
                onClick={() => sort.setSort("disposed_date", "desc")}
              />
              <SortChip
                label="Best alpha"
                active={sort.sortKey === "alpha" && sort.sortDir === "desc"}
                onClick={() => sort.setSort("alpha", "desc")}
              />
              <SortChip
                label="Worst alpha"
                active={sort.sortKey === "alpha" && sort.sortDir === "asc"}
                onClick={() => sort.setSort("alpha", "asc")}
              />
            </div>
            <span className="text-slate-500">
              {sortedRows.length} row{sortedRows.length === 1 ? "" : "s"}
            </span>
          </div>

          <TimelineTable rows={visibleRows} sort={sort} />

          {sortedRows.length > PAGE_SIZE && (
            <div className="border-t border-slate-100 px-4 py-2 text-center">
              <button
                type="button"
                onClick={() => setShowAll((s) => !s)}
                className="text-xs font-medium text-slate-600 hover:text-slate-900"
              >
                {showAll
                  ? `Show top ${PAGE_SIZE}`
                  : `Show all ${sortedRows.length}`}
              </button>
            </div>
          )}

          {data.notes.length > 0 && (
            <ul className="border-t border-slate-100 bg-slate-50 px-4 py-3 text-xs text-slate-600 space-y-1.5">
              {data.notes.map((n, idx) => (
                <li key={idx} className="leading-relaxed">
                  · {n}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

/** Human-readable "Source" label — shared by the column accessor (so sort
 *  matches what's shown) and the row renderer. */
function formatSource(row: TimelineRow): string {
  if (row.row_kind === "open") return "Open";
  if (row.broker) return `${row.broker} ${row.tax_year}`;
  return `1099 ${row.tax_year ?? ""}`;
}

function SummaryStrip({
  summary,
  totalRows,
}: {
  summary: YearSummary | null;
  totalRows: number;
}): JSX.Element {
  if (!summary) {
    return (
      <div className="border-b border-slate-100 px-4 py-3 text-xs text-slate-500">
        No closed-lot summary for selection.
      </div>
    );
  }
  const realized = parseFloat(summary.realized_total);
  const spy = parseFloat(summary.spy_counterfactual_total);
  const alpha = parseFloat(summary.alpha_total);
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-slate-100 px-4 py-3 sm:grid-cols-4">
      <Stat label="Rows" value={totalRows.toLocaleString()} />
      <Stat
        label="Realized P&L"
        value={fmtSignedUSD(realized)}
        valueClass={pnlClass(realized)}
      />
      <Stat
        label="SPY would've done"
        value={fmtSignedUSD(spy)}
        valueClass={pnlClass(spy)}
      />
      <Stat
        label="Alpha vs SPY"
        value={fmtSignedUSD(alpha)}
        valueClass={pnlClass(alpha)}
      />
    </div>
  );
}

function TimelineTable({
  rows,
  sort,
}: {
  rows: TimelineRow[];
  sort: SortControl;
}): JSX.Element {
  if (rows.length === 0) {
    return (
      <div className="px-4 py-6 text-xs text-slate-500">No rows to show.</div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="bg-slate-50 text-slate-600">
          <tr>
            <SortableTh column="disposed_date" sort={sort}>Sold</SortableTh>
            <SortableTh column="ticker" sort={sort}>Ticker</SortableTh>
            <SortableTh column="source" sort={sort}>Source</SortableTh>
            <SortableTh column="held" align="right" sort={sort}>Held</SortableTh>
            <SortableTh column="cost" align="right" sort={sort}>Cost</SortableTh>
            <SortableTh column="pnl" align="right" sort={sort}>P&amp;L</SortableTh>
            <SortableTh column="return" align="right" sort={sort}>Return</SortableTh>
            <SortableTh column="spy" align="right" sort={sort}>SPY P&amp;L</SortableTh>
            <SortableTh column="alpha" align="right" sort={sort}>Alpha</SortableTh>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, idx) => (
            <TimelineTableRow key={`${r.ticker}-${r.disposed_date}-${idx}`} row={r} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TimelineTableRow({ row }: { row: TimelineRow }): JSX.Element {
  const realized = parseFloat(row.realized_gain);
  const cost = parseFloat(row.cost_basis);
  const spyCf = row.spy_counterfactual_dollars
    ? parseFloat(row.spy_counterfactual_dollars)
    : null;
  const alpha = row.alpha_dollars ? parseFloat(row.alpha_dollars) : null;
  const isOpen = row.row_kind === "open";
  const sourceLabel = formatSource(row);
  const fmtDays = (days: number): string =>
    days >= 365 ? `${(days / 365).toFixed(1)}y` : `${days}d`;
  const heldText = fmtDays(row.holding_days);
  // Show dollar-weighted avg below "since first buy" for open positions
  // when the two diverge (otherwise it's just visual noise). Tells the
  // user how long their CAPITAL has actually been deployed, not just
  // how long the position has existed.
  const showWeighted =
    isOpen &&
    Math.abs(row.weighted_avg_holding_days - row.holding_days) > 15;
  return (
    <tr className="border-t border-slate-100 hover:bg-slate-50">
      <Td>{row.disposed_date}</Td>
      <Td>
        <span className="font-medium text-slate-900">{row.ticker ?? "—"}</span>
        {row.description && row.description !== row.ticker ? (
          <div className="text-[10px] text-slate-500 truncate max-w-[180px]">
            {row.description}
          </div>
        ) : null}
      </Td>
      <Td>
        <span
          className={
            isOpen
              ? "rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-800"
              : "text-[11px] text-slate-600"
          }
        >
          {sourceLabel}
        </span>
        {row.term ? (
          <span className="ml-1 text-[10px] uppercase text-slate-400">
            {row.term}
          </span>
        ) : null}
      </Td>
      <Td align="right">
        <span title={row.acquired_approx ? "Acquired date approximated" : ""}>
          {heldText}
          {row.acquired_approx ? "*" : ""}
        </span>
        {showWeighted && (
          <div
            className="text-[10px] text-slate-500"
            title="Dollar-weighted average holding days across all buys. SPY counterfactual uses this matched-flow timing, not the first-buy date."
          >
            $-wt {fmtDays(row.weighted_avg_holding_days)}
          </div>
        )}
      </Td>
      <Td align="right">{fmtUSD(cost)}</Td>
      <Td align="right" className={pnlClass(realized)}>
        {fmtSignedUSD(realized)}
      </Td>
      <Td align="right" className={pnlClass(row.return_pct)}>
        {row.return_pct !== null
          ? `${(row.return_pct * 100).toFixed(1)}%`
          : "—"}
      </Td>
      <Td align="right" className={pnlClass(spyCf)}>
        {spyCf !== null ? fmtSignedUSD(spyCf) : "—"}
      </Td>
      <Td align="right" className={`font-semibold ${pnlClass(alpha)}`}>
        {alpha !== null ? fmtSignedUSD(alpha) : "—"}
      </Td>
    </tr>
  );
}

function Stat({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}): JSX.Element {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
      <div
        className={[
          "mt-0.5 text-sm font-semibold tabular-nums truncate",
          valueClass ?? "text-slate-900",
        ].join(" ")}
      >
        {value}
      </div>
    </div>
  );
}

function YearChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
        active
          ? "bg-slate-900 text-white"
          : "bg-slate-100 text-slate-700 hover:bg-slate-200"
      }`}
    >
      {label}
    </button>
  );
}

function SortChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-2 py-1 text-[11px] transition-colors ${
        active
          ? "bg-slate-200 text-slate-900 font-medium"
          : "text-slate-600 hover:bg-slate-100"
      }`}
    >
      {label}
    </button>
  );
}
