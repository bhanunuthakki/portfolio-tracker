import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { DecisionLogCard } from "@/components/DecisionLogCard";
import { PositionAlphaChart } from "@/components/PositionAlphaChart";
import { PositionAlphaTable } from "@/components/PositionAlphaTable";
import { RiskMetricsCard } from "@/components/RiskMetricsCard";
import { Card, ErrorBanner, Stat } from "@/components/ui";

type RangePreset = "1M" | "3M" | "6M" | "YTD" | "1Y" | "2Y" | "MAX" | "CUSTOM";

const PRESETS: { key: Exclude<RangePreset, "CUSTOM">; label: string }[] = [
  { key: "1M", label: "1M" },
  { key: "3M", label: "3M" },
  { key: "6M", label: "6M" },
  { key: "YTD", label: "YTD" },
  { key: "1Y", label: "1Y" },
  { key: "2Y", label: "2Y" },
  { key: "MAX", label: "Max" },
];

// Dollar amount of "untouchable" emergency cash reserve. When the user
// toggles "Exclude reserve" on the dashboard, this much is carved off the
// top of V_start, every daily V, AND each synthetic-benchmark base before
// returns are computed — so the chart compares the *investable* portion
// against an equivalent-sized index allocation.
const CASH_RESERVE_AMOUNT = 30000;

function isoDaysBefore(anchorISO: string, days: number): string {
  const d = new Date(anchorISO + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

function isoYTDFor(anchorISO: string): string {
  return `${anchorISO.slice(0, 4)}-01-01`;
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function presetToDates(
  preset: Exclude<RangePreset, "CUSTOM">,
  endAnchor: string,
): {
  startDate?: string;
  endDate?: string;
  includeBackfill: boolean;
} {
  const endDate = endAnchor === todayISO() ? undefined : endAnchor;
  switch (preset) {
    case "1M":
      return { startDate: isoDaysBefore(endAnchor, 30), endDate, includeBackfill: true };
    case "3M":
      return { startDate: isoDaysBefore(endAnchor, 90), endDate, includeBackfill: true };
    case "6M":
      return { startDate: isoDaysBefore(endAnchor, 180), endDate, includeBackfill: true };
    case "YTD":
      return { startDate: isoYTDFor(endAnchor), endDate, includeBackfill: true };
    case "1Y":
      return { startDate: isoDaysBefore(endAnchor, 365), endDate, includeBackfill: true };
    case "2Y":
      return { startDate: isoDaysBefore(endAnchor, 730), endDate, includeBackfill: true };
    case "MAX":
      return { endDate, includeBackfill: true };
  }
}

export function Dashboard(): JSX.Element {
  const [preset, setPreset] = useState<RangePreset>("1Y");
  // End-date anchor — defaults to today but the user can set it to any
  // past date to view the dashboard "as of" that date. All presets
  // (1M / 3M / etc.) compute their start date relative to this anchor.
  const [endAnchor, setEndAnchor] = useState<string>(todayISO());
  const [customStart, setCustomStart] = useState<string>(
    isoDaysBefore(todayISO(), 365),
  );
  const [excludeReserve, setExcludeReserve] = useState<boolean>(false);
  const [excludeIndexEtfs, setExcludeIndexEtfs] = useState<boolean>(false);
  const endIsToday = endAnchor === todayISO();

  const params = useMemo(() => {
    if (preset === "CUSTOM") {
      return {
        startDate: customStart,
        endDate: endAnchor,
        includeBackfill: true,
      };
    }
    return presetToDates(preset, endAnchor);
  }, [preset, customStart, endAnchor]);

  const reserveAmount = excludeReserve ? CASH_RESERVE_AMOUNT : 0;

  const performance = useQuery({
    queryKey: ["performance", params, reserveAmount, excludeIndexEtfs],
    queryFn: () =>
      api.performance({
        startDate: params.startDate,
        endDate: params.endDate,
        includeBackfill: params.includeBackfill,
        reserveAmount,
        excludeIndexEtfs,
      }),
  });
  const positionAlpha = useQuery({
    queryKey: ["position-alpha", params.startDate, params.endDate],
    queryFn: () =>
      api.positionAlpha({
        startDate: params.startDate,
        endDate: params.endDate,
      }),
  });
  const holdings = useQuery({
    queryKey: ["holdings"],
    queryFn: () => api.latestHoldings(),
  });

  const totalValue = holdings.data
    ? holdings.data.reduce(
        (sum, h) => sum + (h.total_value !== null ? parseFloat(h.total_value) : 0),
        0,
      )
    : null;

  const cashflowIn = performance.data
    ? parseFloat(performance.data.net_external_cashflow_in)
    : null;
  const baseValue = performance.data ? parseFloat(performance.data.base_value) : null;
  const totalAlpha = positionAlpha.data
    ? parseFloat(positionAlpha.data.total_alpha)
    : null;
  const totalActualPl = positionAlpha.data
    ? parseFloat(positionAlpha.data.total_actual_pl)
    : null;
  const totalSpyPl = positionAlpha.data
    ? parseFloat(positionAlpha.data.total_spy_pl)
    : null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Portfolio value"
          value={
            totalValue !== null
              ? `$${totalValue.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
              : "—"
          }
        />
        <Stat
          label="Net contributions (window)"
          value={
            cashflowIn !== null
              ? `${cashflowIn >= 0 ? "+" : "-"}$${Math.round(Math.abs(cashflowIn)).toLocaleString()}`
              : "—"
          }
        />
        <Stat
          label="$ alpha vs SPY"
          value={
            totalAlpha !== null
              ? `${totalAlpha >= 0 ? "+" : "−"}$${Math.round(Math.abs(totalAlpha)).toLocaleString()}`
              : "—"
          }
          mono
        />
        <Stat
          label="Range"
          value={
            positionAlpha.data
              ? `${positionAlpha.data.start_date} → ${positionAlpha.data.end_date}`
              : performance.data
              ? `${performance.data.start_date} → ${performance.data.end_date}`
              : "—"
          }
          mono
        />
      </div>

      {performance.data?.backfill_start_unreliable && (
        <BackfillWarning
          baseValue={baseValue}
          endValue={totalValue}
          earliestObserved={performance.data.earliest_observed_date}
        />
      )}

      <Card>
        <header className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <h2 className="text-sm font-semibold text-slate-900 shrink-0">
            Performance vs benchmarks
            {(excludeReserve || excludeIndexEtfs) && (
              <span className="ml-2 rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-800 align-middle">
                {excludeIndexEtfs ? "active picks only" : "ex-reserve"}
              </span>
            )}
          </h2>
          <div className="flex flex-wrap items-center gap-2 lg:flex-1 lg:justify-center lg:px-4">
            <ToggleChip
              active={excludeReserve}
              onClick={() => setExcludeReserve(!excludeReserve)}
              activeColor="emerald"
              label="Excl. $30k SGOV reserve"
              tooltip="Treat $30k of cash as untouchable emergency reserves; carve from V_start, every daily V, and the synthetic-benchmark base before computing returns."
            />
            <ToggleChip
              active={excludeIndexEtfs}
              onClick={() => setExcludeIndexEtfs(!excludeIndexEtfs)}
              activeColor="indigo"
              label="Excl. broad-index ETFs"
              tooltip="Strip VTI/VOO/SPY/IVV/RSP from V and add their buy/sell flows to the cashflow series. Isolates active stock-picking alpha."
            />
          </div>
          <div className="flex flex-wrap items-center gap-1 shrink-0">
            {PRESETS.map((p) => (
              <RangeChip
                key={p.key}
                label={p.label}
                active={preset === p.key}
                onClick={() => setPreset(p.key)}
              />
            ))}
            <RangeChip
              label="Custom"
              active={preset === "CUSTOM"}
              onClick={() => setPreset("CUSTOM")}
            />
          </div>
        </header>

        <div className="flex flex-wrap items-end gap-3 px-4 py-2.5 border-b border-slate-100 bg-slate-50/60">
          {preset === "CUSTOM" && (
            <label className="flex flex-col text-xs text-slate-600">
              Start date
              <input
                type="date"
                value={customStart}
                onChange={(e) => setCustomStart(e.target.value)}
                max={endAnchor}
                className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
              />
            </label>
          )}
          <label className="flex flex-col text-xs text-slate-600">
            End date {!endIsToday && <span className="text-amber-700 font-medium">(historical)</span>}
            <input
              type="date"
              value={endAnchor}
              onChange={(e) => setEndAnchor(e.target.value || todayISO())}
              max={todayISO()}
              min={preset === "CUSTOM" ? customStart : undefined}
              className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
              title="All presets compute relative to this date. Pick a past date to view the dashboard as of that date."
            />
          </label>
          {!endIsToday && (
            <button
              type="button"
              onClick={() => setEndAnchor(todayISO())}
              className="rounded border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100"
              title="Reset end date to today"
            >
              ↺ Today
            </button>
          )}
        </div>

        <div className="px-4 py-3">
          {positionAlpha.isLoading ? (
            <div className="text-xs text-slate-500">Loading…</div>
          ) : positionAlpha.isError ? (
            <ErrorBanner>Failed to load position-alpha series.</ErrorBanner>
          ) : positionAlpha.data ? (
            <>
              <PositionAlphaChart data={positionAlpha.data} />
              <div className="mt-3 text-xs text-slate-500 leading-relaxed">
                <strong>Method:</strong> each ticker held on{" "}
                {positionAlpha.data.start_date} starts the window at{" "}
                qty × that day's close. SPY counterfactual invests the same
                starting capital in SPY at that day's SPY close, then
                dollar-matches every in-window buy/sell. Alpha at end ={" "}
                <strong>
                  {totalAlpha !== null
                    ? `${totalAlpha >= 0 ? "+" : "−"}$${Math.round(Math.abs(totalAlpha)).toLocaleString()}`
                    : "—"}
                </strong>{" "}
                ({totalActualPl !== null && totalSpyPl !== null
                  ? `actual ${totalActualPl >= 0 ? "+" : "−"}$${Math.round(Math.abs(totalActualPl)).toLocaleString()} vs SPY ${totalSpyPl >= 0 ? "+" : "−"}$${Math.round(Math.abs(totalSpyPl)).toLocaleString()}`
                  : "—"})
              </div>
            </>
          ) : null}
        </div>
      </Card>

      {positionAlpha.data && positionAlpha.data.rows.length > 0 && (
        <Card>
          <header className="border-b border-slate-100 px-4 py-3">
            <h2 className="text-sm font-semibold text-slate-900">
              Per-position alpha vs SPY
              <span className="ml-2 text-xs font-normal text-slate-500">
                ranked best-to-worst by alpha (descending). Click a ticker for detail.
              </span>
            </h2>
          </header>
          <PositionAlphaTable data={positionAlpha.data} />
        </Card>
      )}

      <RiskMetricsCard
        startDate={params.startDate}
        endDate={params.endDate}
        includeBackfill={params.includeBackfill}
        excludeIndexEtfs={excludeIndexEtfs}
        reserveAmount={reserveAmount}
      />

      <DecisionLogCard />
    </div>
  );
}

function BackfillWarning({
  baseValue,
  endValue,
  earliestObserved,
}: {
  baseValue: number | null;
  endValue: number | null;
  earliestObserved: string | null;
}): JSX.Element {
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
      <div className="font-medium">
        ⚠ The portfolio % line over this window is unreliable.
      </div>
      <p className="mt-1 text-xs leading-relaxed">
        The starting value is reconstructed by walking your transaction
        history backward (Plaid only retains 24 months). The reconstruction
        tracks positions but not cash, so it collapses the apparent starting
        portfolio to{" "}
        {baseValue !== null
          ? `$${Math.round(baseValue).toLocaleString()}`
          : "an unknown amount"}{" "}
        when the actual end value is{" "}
        {endValue !== null ? `$${Math.round(endValue).toLocaleString()}` : "—"}
        . With a tiny start, every dollar of gain reads as a huge percentage.
      </p>
      <p className="mt-2 text-xs leading-relaxed">
        The SPY / QQQ <em>matched-flow</em> lines use the same wrong start,
        so they understate too — but proportionally less, because synthetic
        index portfolios grow at known market rates. The <strong>relative
        ordering</strong> (portfolio vs index) is correct; the <strong>absolute
        gap</strong> is exaggerated.
      </p>
      <p className="mt-2 text-xs leading-relaxed">
        <strong>Fully trustworthy:</strong> current portfolio value,
        contributions total, raw SPY / QQQ market returns. <strong>Becomes
        trustworthy:</strong> the portfolio % line{" "}
        {earliestObserved
          ? `from ${earliestObserved} forward`
          : "once daily snapshots accumulate"}
        . Schedule the snapshot job nightly.
      </p>
    </div>
  );
}

function RangeChip({
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
      className={[
        "rounded px-2.5 py-1 text-xs font-medium transition-colors",
        active
          ? "bg-slate-900 text-white"
          : "text-slate-600 hover:bg-slate-100",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

/**
 * Pill-styled toggle that fills the white space between the chart title
 * and the range-preset chips. Two of these sit side-by-side on the
 * dashboard — one for the SGOV cash-reserve carve-out, one for the
 * broad-index ETF carve-out. Active state reads as a colored pill so the
 * effect on the chart is unambiguous when scanning quickly.
 *
 * `tooltip` ends up on the native `title` attribute — short, hover-only,
 * doesn't compete with the page layout. Click target is large enough to
 * comfortably hit on touch (≥32px tall via py-1.5 + text + border).
 */
function ToggleChip({
  active,
  onClick,
  activeColor,
  label,
  tooltip,
}: {
  active: boolean;
  onClick: () => void;
  activeColor: "emerald" | "indigo";
  label: string;
  tooltip?: string;
}): JSX.Element {
  const activeClasses =
    activeColor === "emerald"
      ? "border-emerald-600 bg-emerald-50 text-emerald-800 hover:bg-emerald-100"
      : "border-indigo-600 bg-indigo-50 text-indigo-800 hover:bg-indigo-100";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={active}
      onClick={onClick}
      title={tooltip}
      className={[
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
        active
          ? activeClasses
          : "border-slate-300 bg-white text-slate-600 hover:border-slate-400 hover:bg-slate-50",
      ].join(" ")}
    >
      <span
        aria-hidden="true"
        className={[
          "inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border",
          active
            ? activeColor === "emerald"
              ? "border-emerald-600 bg-emerald-600 text-white"
              : "border-indigo-600 bg-indigo-600 text-white"
            : "border-slate-400 bg-white",
        ].join(" ")}
      >
        {active ? "✓" : ""}
      </span>
      <span>{label}</span>
    </button>
  );
}
