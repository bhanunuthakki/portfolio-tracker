import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { Card, Td, Th, fmtSignedUSD, fmtUSD, pnlClass } from "@/components/ui";
import type { TimelineRow, YearSummary } from "@/types";

/**
 * Chronological trade timeline with SPY counterfactual.
 *
 * For each closed lot (1099 data) and each currently-held position (broker
 * snapshot), we compare the realized/unrealized gain to what SPY would have
 * returned over the same holding window. Alpha = excess vs SPY.
 *
 * Sort modes:
 *   - "recent"     — disposed_date desc (default; "what did I do lately")
 *   - "best-alpha" — alpha desc          ("my biggest wins vs index")
 *   - "worst-alpha"— alpha asc           ("which trades cost me")
 */
type SortMode = "recent" | "best-alpha" | "worst-alpha";

const PAGE_SIZE = 25;

export function TradeTimelineCard(): JSX.Element {
  const [year, setYear] = useState<number | "all">("all");
  const [sortMode, setSortMode] = useState<SortMode>("recent");
  const [showAll, setShowAll] = useState<boolean>(false);

  // Fetch the full timeline once; year filter is client-side so the chip
  // bar stays stable as the user navigates between years.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["trade-timeline"],
    queryFn: () => api.tradeTimeline({ includeOpen: true }),
  });

  const sortedRows = useMemo<TimelineRow[]>(() => {
    if (!data) return [];
    const rows = data.rows.filter((r) => {
      if (year === "all") return true;
      // Open positions only show when "All" is selected — they don't belong
      // to any single tax year.
      if (r.row_kind === "open") return false;
      return r.tax_year === year;
    });
    if (sortMode === "recent") {
      rows.sort((a, b) => b.disposed_date.localeCompare(a.disposed_date));
    } else if (sortMode === "best-alpha") {
      rows.sort((a, b) => parseAlpha(b) - parseAlpha(a));
    } else {
      rows.sort((a, b) => parseAlpha(a) - parseAlpha(b));
    }
    return rows;
  }, [data, sortMode, year]);

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
              <SortChip
                label="Recent"
                active={sortMode === "recent"}
                onClick={() => setSortMode("recent")}
              />
              <SortChip
                label="Best alpha"
                active={sortMode === "best-alpha"}
                onClick={() => setSortMode("best-alpha")}
              />
              <SortChip
                label="Worst alpha"
                active={sortMode === "worst-alpha"}
                onClick={() => setSortMode("worst-alpha")}
              />
            </div>
            <span className="text-slate-500">
              {sortedRows.length} row{sortedRows.length === 1 ? "" : "s"}
            </span>
          </div>

          <TimelineTable rows={visibleRows} />

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

function parseAlpha(r: TimelineRow): number {
  return r.alpha_dollars === null ? 0 : parseFloat(r.alpha_dollars);
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

function TimelineTable({ rows }: { rows: TimelineRow[] }): JSX.Element {
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
            <Th>Sold</Th>
            <Th>Ticker</Th>
            <Th>Source</Th>
            <Th align="right">Held</Th>
            <Th align="right">Cost</Th>
            <Th align="right">P&amp;L</Th>
            <Th align="right">Return</Th>
            <Th align="right">SPY P&amp;L</Th>
            <Th align="right">Alpha</Th>
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
  const sourceLabel = isOpen
    ? "Open"
    : row.broker
      ? `${row.broker} ${row.tax_year}`
      : `1099 ${row.tax_year ?? ""}`;
  const heldText =
    row.holding_days >= 365
      ? `${(row.holding_days / 365).toFixed(1)}y`
      : `${row.holding_days}d`;
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
