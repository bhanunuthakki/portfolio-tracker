import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import {
  filterBySearch,
  useTableSort,
} from "@/components/useTableSort";
import {
  Card,
  CLASSIFICATION_CHIP_CLASSES as CLASSIFICATION_CHIP,
  CLASSIFICATION_LABELS as CLASSIFICATION_LABEL,
  fmtSignedUSD,
  pnlClass,
} from "@/components/ui";
import type { InvestmentTransactionOut } from "@/types";

/**
 * Standalone "Contribution detail" page with sort + search.
 *
 * Replaces the small dashboard card. Same data (cashflow tx for the
 * selected window) but with full sortable columns, a search box that
 * matches across date / account / description / classification, and
 * date range pickers for ad-hoc analysis.
 */

type Preset = "6M" | "1Y" | "2Y" | "5Y" | "MAX";

const PRESETS: { key: Preset; label: string; days: number | null }[] = [
  { key: "6M", label: "6M", days: 180 },
  { key: "1Y", label: "1Y", days: 365 },
  { key: "2Y", label: "2Y", days: 730 },
  { key: "5Y", label: "5Y", days: 1825 },
  { key: "MAX", label: "Max", days: null },
];

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function Contributions(): JSX.Element {
  const [preset, setPreset] = useState<Preset>("1Y");
  const [search, setSearch] = useState("");

  const startDate = useMemo(() => {
    const p = PRESETS.find((x) => x.key === preset);
    return p?.days ? isoDaysAgo(p.days) : undefined;
  }, [preset]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions", { startDate, limit: 2000, page: "contributions" }],
    queryFn: () =>
      api.transactions({ startDate, limit: 2000 }),
  });

  const cashflowRows = useMemo(
    () =>
      (data ?? []).filter((t) => t.effective_classification !== null),
    [data],
  );

  const filtered = useMemo(
    () =>
      filterBySearch(cashflowRows, search, [
        (t) => t.date,
        (t) => t.account_name,
        (t) => t.type,
        (t) => t.subtype,
        (t) => t.ticker,
        (t) => t.name,
        (t) => t.effective_classification
          ? CLASSIFICATION_LABEL[t.effective_classification]
          : null,
      ]),
    [cashflowRows, search],
  );

  const accessors = useMemo(
    () => ({
      date: (t: InvestmentTransactionOut) => t.date,
      account: (t: InvestmentTransactionOut) => t.account_name,
      type: (t: InvestmentTransactionOut) => t.subtype ?? t.type,
      name: (t: InvestmentTransactionOut) => t.name ?? "",
      amount: (t: InvestmentTransactionOut) => parseFloat(t.amount),
      abs: (t: InvestmentTransactionOut) => Math.abs(parseFloat(t.amount)),
      classification: (t: InvestmentTransactionOut) =>
        t.effective_classification ?? "",
    }),
    [],
  );

  const { sortedRows, toggle, sortIndicator } = useTableSort(
    filtered,
    "abs",
    "desc",
    accessors,
  );

  const stats = useMemo(() => {
    let inflow = 0,
      outflow = 0,
      internalCount = 0,
      overridden = 0;
    filtered.forEach((t) => {
      const amt = Math.abs(parseFloat(t.amount));
      if (t.effective_classification === "external_in") inflow += amt;
      else if (t.effective_classification === "external_out") outflow += amt;
      else if (t.effective_classification === "internal") internalCount++;
      if (t.override_classification !== null) overridden++;
    });
    return {
      inflow,
      outflow,
      net: inflow - outflow,
      internalCount,
      overridden,
    };
  }, [filtered]);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg font-semibold text-slate-900">
          Contribution detail
        </h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Every cashflow transaction in the selected window. Click any header
          to sort; type to filter. Override misclassified rows on the
          Transactions page.
        </p>
      </header>

      <Card>
        <div className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-1">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setPreset(p.key)}
                className={`rounded px-2 py-1 text-[11px] font-medium ${
                  preset === p.key
                    ? "bg-slate-900 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <input
            type="search"
            placeholder="Search date, account, ticker, description…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded border border-slate-200 px-2 py-1 text-xs sm:w-72"
          />
        </div>

        {isLoading ? (
          <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
        ) : isError ? (
          <div className="px-4 py-6 text-xs text-red-700">
            Failed to load.
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-slate-100 px-4 py-3 sm:grid-cols-4">
              <StatBlock
                label={`Net (${filtered.length} rows)`}
                value={fmtSignedUSD(stats.net)}
                valueClass={pnlClass(stats.net)}
              />
              <StatBlock
                label="Inflows"
                value={`+$${stats.inflow.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                valueClass="text-emerald-700"
              />
              <StatBlock
                label="Outflows"
                value={`-$${stats.outflow.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                valueClass="text-rose-700"
              />
              <StatBlock
                label="Internal / overridden"
                value={`${stats.internalCount} / ${stats.overridden}`}
              />
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <Sh onClick={() => toggle("date")}>
                      Date{sortIndicator("date")}
                    </Sh>
                    <Sh onClick={() => toggle("account")}>
                      Account{sortIndicator("account")}
                    </Sh>
                    <Sh onClick={() => toggle("type")}>
                      Type{sortIndicator("type")}
                    </Sh>
                    <Sh onClick={() => toggle("name")}>
                      Description{sortIndicator("name")}
                    </Sh>
                    <Sh align="right" onClick={() => toggle("abs")}>
                      Amount{sortIndicator("abs")}
                    </Sh>
                    <Sh onClick={() => toggle("classification")}>
                      Counted as{sortIndicator("classification")}
                    </Sh>
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="px-4 py-6 text-center text-slate-500">
                        No matching rows.
                      </td>
                    </tr>
                  ) : (
                    sortedRows.map((t) => {
                      const amt = parseFloat(t.amount);
                      const eff = t.effective_classification;
                      const signed =
                        eff === "external_in"
                          ? Math.abs(amt)
                          : eff === "external_out"
                            ? -Math.abs(amt)
                            : amt;
                      return (
                        <tr
                          key={t.plaid_investment_transaction_id}
                          className="border-t border-slate-100 hover:bg-slate-50"
                        >
                          <td className="px-3 py-1.5 tabular-nums text-slate-900">
                            {t.date}
                          </td>
                          <td className="px-3 py-1.5 text-slate-700">
                            {t.account_name}
                          </td>
                          <td className="px-3 py-1.5">
                            <span className="inline-flex rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-700">
                              {t.subtype ?? t.type}
                            </span>
                          </td>
                          <td className="px-3 py-1.5 text-slate-600 max-w-[280px]">
                            <span className="block truncate" title={t.name ?? ""}>
                              {t.name ?? "—"}
                            </span>
                          </td>
                          <td className={`px-3 py-1.5 text-right tabular-nums ${pnlClass(signed)}`}>
                            {fmtSignedUSD(signed)}
                          </td>
                          <td className="px-3 py-1.5">
                            {eff !== null ? (
                              <span
                                className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${CLASSIFICATION_CHIP[eff]}`}
                              >
                                {CLASSIFICATION_LABEL[eff]}
                                {t.override_classification !== null ? (
                                  <span className="text-[8px] uppercase opacity-70">
                                    override
                                  </span>
                                ) : null}
                              </span>
                            ) : (
                              <span className="text-slate-400">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

function Sh({
  children,
  align,
  onClick,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  onClick: () => void;
}): JSX.Element {
  return (
    <th
      onClick={onClick}
      className={[
        "cursor-pointer select-none px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-600 hover:bg-slate-100",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

function StatBlock({
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
