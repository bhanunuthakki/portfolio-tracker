import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import { CashflowRuleAudit } from "@/components/CashflowRuleAudit";
import {
  filterBySearch,
  useTableSort,
} from "@/components/useTableSort";
import {
  Card,
  CLASSIFICATION_CHIP_CLASSES,
  CLASSIFICATION_LABELS,
  EmptyState,
  ErrorBanner,
  SortableTh,
} from "@/components/ui";
import type { InvestmentTransactionOut, TxClassification } from "@/types";

/**
 * Transactions table with inline + bulk cashflow-classification overrides.
 *
 * Each row has a "Counted as" chip showing what the TWR / contributions
 * pipeline does with it. Click the chip to edit one row. Or check rows
 * and use the bulk action bar to tag many at once — useful for cleaning
 * up ACATS pairs (mark both legs as internal in one batch).
 *
 * Sortable columns + free-text search filter across date / account /
 * type / ticker / description / classification.
 */

type Filter = "all" | "cashflow" | "overridden";
type RangePreset = "6M" | "1Y" | "2Y" | "5Y" | "MAX";

const FILTER_LABELS: Record<Filter, string> = {
  all: "All",
  cashflow: "Cashflow rows",
  overridden: "Overridden only",
};

const RANGE_PRESETS: { key: RangePreset; label: string; days: number | null }[] = [
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

const CLASSIFICATION_BTN_CLASSES: Record<TxClassification, string> = {
  external_in: "bg-emerald-600 hover:bg-emerald-700 text-white",
  external_out: "bg-rose-600 hover:bg-rose-700 text-white",
  internal: "bg-slate-700 hover:bg-slate-800 text-white",
};

export function Transactions(): JSX.Element {
  const [filter, setFilter] = useState<Filter>("all");
  const [rangePreset, setRangePreset] = useState<RangePreset>("2Y");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const startDate = useMemo(() => {
    const p = RANGE_PRESETS.find((x) => x.key === rangePreset);
    // For "Max" we pass an explicit ancient date so the backend doesn't
    // fall back to its 730-day default cap when start_date is None.
    return p?.days ? isoDaysAgo(p.days) : "2000-01-01";
  }, [rangePreset]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions", { startDate, limit: 5000 }],
    queryFn: () => api.transactions({ startDate, limit: 5000 }),
  });

  const baseFiltered = useMemo(() => {
    if (!data) return [];
    return data.filter((t) => {
      if (filter === "all") return true;
      if (filter === "overridden") return t.override_classification !== null;
      if (filter === "cashflow") return t.effective_classification !== null;
      return true;
    });
  }, [data, filter]);

  const searchFiltered = useMemo(
    () =>
      filterBySearch(baseFiltered, search, [
        (t) => t.date,
        (t) => t.account_name,
        (t) => t.type,
        (t) => t.subtype,
        (t) => t.ticker,
        (t) => t.name,
        (t) => t.amount,
        (t) =>
          t.effective_classification
            ? CLASSIFICATION_LABELS[t.effective_classification]
            : null,
      ]),
    [baseFiltered, search],
  );

  const accessors = useMemo(
    () => ({
      date: (t: InvestmentTransactionOut) => t.date,
      account: (t: InvestmentTransactionOut) => t.account_name,
      type: (t: InvestmentTransactionOut) => t.subtype ?? t.type,
      ticker: (t: InvestmentTransactionOut) => t.ticker ?? "",
      name: (t: InvestmentTransactionOut) => t.name ?? "",
      quantity: (t: InvestmentTransactionOut) => parseFloat(t.quantity),
      amount: (t: InvestmentTransactionOut) => parseFloat(t.amount),
      classification: (t: InvestmentTransactionOut) =>
        t.effective_classification ?? "",
    }),
    [],
  );

  const sort = useTableSort(searchFiltered, "date", "desc", accessors);
  const { sortedRows } = sort;

  const allVisibleIds = useMemo(
    () => sortedRows.map((t) => t.plaid_investment_transaction_id),
    [sortedRows],
  );

  const allSelected =
    allVisibleIds.length > 0 &&
    allVisibleIds.every((id) => selectedIds.has(id));

  const overriddenCount = (data ?? []).filter(
    (t) => t.override_classification !== null,
  ).length;

  if (isLoading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (isError)
    return <ErrorBanner>Failed to load transactions.</ErrorBanner>;
  if (!data || data.length === 0) {
    return (
      <EmptyState>
        No transactions yet. Link an account and run the backfill job.
      </EmptyState>
    );
  }

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (allSelected) setSelectedIds(new Set());
    else setSelectedIds(new Set(allVisibleIds));
  }

  return (
    <div className="space-y-3">
      <CashflowRuleAudit />
      <header className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Transactions</h1>
          <div className="text-xs text-slate-500">
            {data.length} loaded · {overriddenCount} overridden ·{" "}
            {sortedRows.length} visible
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1">
            {RANGE_PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setRangePreset(p.key)}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  rangePreset === p.key
                    ? "bg-slate-200 text-slate-900"
                    : "text-slate-600 hover:bg-slate-100"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <span className="text-slate-300">|</span>
          <div className="flex items-center gap-1">
            {(["all", "cashflow", "overridden"] as Filter[]).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                  filter === f
                    ? "bg-slate-900 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {FILTER_LABELS[f]}
              </button>
            ))}
          </div>
          <input
            type="search"
            placeholder="Search date / account / ticker / desc…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded border border-slate-200 px-2 py-1 text-xs sm:w-64"
          />
        </div>
      </header>

      {selectedIds.size > 0 && (
        <BulkActionBar
          selectedIds={selectedIds}
          onClear={() => setSelectedIds(new Set())}
        />
      )}

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="w-8 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleSelectAll}
                    aria-label="Select all visible"
                  />
                </th>
                <SortableTh column="date" sort={sort} pad="tight">
                  Date
                </SortableTh>
                <SortableTh column="account" sort={sort} pad="tight">
                  Account
                </SortableTh>
                <SortableTh column="type" sort={sort} pad="tight">
                  Type
                </SortableTh>
                <SortableTh column="ticker" sort={sort} pad="tight">
                  Ticker
                </SortableTh>
                <SortableTh column="name" sort={sort} pad="tight">
                  Description
                </SortableTh>
                <SortableTh column="quantity" align="right" sort={sort} pad="tight">
                  Quantity
                </SortableTh>
                <SortableTh column="amount" align="right" sort={sort} pad="tight">
                  Amount
                </SortableTh>
                <SortableTh column="classification" sort={sort} pad="tight">
                  Counted as
                </SortableTh>
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((t) => (
                <TxRow
                  key={t.plaid_investment_transaction_id}
                  tx={t}
                  selected={selectedIds.has(t.plaid_investment_transaction_id)}
                  onToggleSelect={() =>
                    toggleSelect(t.plaid_investment_transaction_id)
                  }
                  isEditing={
                    editingId === t.plaid_investment_transaction_id
                  }
                  onStartEdit={() =>
                    setEditingId(t.plaid_investment_transaction_id)
                  }
                  onCloseEdit={() => setEditingId(null)}
                />
              ))}
            </tbody>
          </table>
        </div>
        <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
          Click any header to sort. Click the &ldquo;Counted as&rdquo; chip to
          override one row, or check rows and use the bulk action bar.
        </p>
      </Card>
    </div>
  );
}

function BulkActionBar({
  selectedIds,
  onClear,
}: {
  selectedIds: Set<string>;
  onClear: () => void;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [isPending, setIsPending] = useState(false);

  async function applyToAll(classification: TxClassification | "clear") {
    setIsPending(true);
    try {
      await Promise.all(
        Array.from(selectedIds).map((id) =>
          classification === "clear"
            ? api.deleteTransactionOverride(id).catch(() => null) // 404 is fine
            : api.setTransactionOverride({
                plaid_investment_transaction_id: id,
                classification,
              }),
        ),
      );
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      onClear();
    } finally {
      setIsPending(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-2 shadow-sm">
      <span className="text-xs font-medium text-slate-700">
        {selectedIds.size} selected · mark as:
      </span>
      {(["external_in", "external_out", "internal"] as TxClassification[]).map(
        (c) => (
          <button
            key={c}
            type="button"
            disabled={isPending}
            onClick={() => applyToAll(c)}
            className={`rounded px-2 py-1 text-[11px] font-medium disabled:opacity-50 ${CLASSIFICATION_BTN_CLASSES[c]}`}
          >
            {CLASSIFICATION_LABELS[c]}
          </button>
        ),
      )}
      <button
        type="button"
        disabled={isPending}
        onClick={() => applyToAll("clear")}
        className="rounded border border-slate-300 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
        title="Remove overrides on selected rows"
      >
        Clear override
      </button>
      <button
        type="button"
        onClick={onClear}
        className="ml-auto text-[11px] text-slate-500 hover:text-slate-900"
      >
        Deselect
      </button>
    </div>
  );
}

function TxRow({
  tx,
  selected,
  onToggleSelect,
  isEditing,
  onStartEdit,
  onCloseEdit,
}: {
  tx: InvestmentTransactionOut;
  selected: boolean;
  onToggleSelect: () => void;
  isEditing: boolean;
  onStartEdit: () => void;
  onCloseEdit: () => void;
}): JSX.Element {
  return (
    <tr
      className={
        selected
          ? "border-t border-slate-100 bg-indigo-50/50"
          : "border-t border-slate-100 hover:bg-slate-50"
      }
    >
      <td className="px-3 py-1.5">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          aria-label="Select row"
        />
      </td>
      <td className="px-3 py-1.5 tabular-nums text-slate-900">{tx.date}</td>
      <td className="px-3 py-1.5 text-slate-700">{tx.account_name}</td>
      <td className="px-3 py-1.5">
        <span className="inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-slate-700">
          {tx.subtype ?? tx.type}
        </span>
      </td>
      <td className="px-3 py-1.5 font-mono text-slate-900">
        {tx.ticker ?? "—"}
      </td>
      <td className="px-3 py-1.5 text-slate-600">
        <span className="block max-w-[280px] truncate" title={tx.name ?? ""}>
          {tx.name ?? "—"}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
        {parseFloat(tx.quantity).toLocaleString(undefined, {
          maximumFractionDigits: 4,
        })}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
        ${Math.round(parseFloat(tx.amount)).toLocaleString()}
      </td>
      <td className="px-3 py-1.5">
        <ClassificationCell
          tx={tx}
          isEditing={isEditing}
          onStartEdit={onStartEdit}
          onCloseEdit={onCloseEdit}
        />
      </td>
    </tr>
  );
}

function ClassificationCell({
  tx,
  isEditing,
  onStartEdit,
  onCloseEdit,
}: {
  tx: InvestmentTransactionOut;
  isEditing: boolean;
  onStartEdit: () => void;
  onCloseEdit: () => void;
}): JSX.Element {
  const queryClient = useQueryClient();
  const setMutation = useMutation({
    mutationFn: (classification: TxClassification) =>
      api.setTransactionOverride({
        plaid_investment_transaction_id: tx.plaid_investment_transaction_id,
        classification,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      onCloseEdit();
    },
  });
  const clearMutation = useMutation({
    mutationFn: () =>
      api.deleteTransactionOverride(tx.plaid_investment_transaction_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      onCloseEdit();
    },
  });
  const isPending = setMutation.isPending || clearMutation.isPending;

  if (isEditing) {
    return (
      <div className="flex flex-wrap items-center gap-1">
        {(["external_in", "external_out", "internal"] as TxClassification[]).map(
          (c) => (
            <button
              key={c}
              type="button"
              disabled={isPending}
              onClick={() => setMutation.mutate(c)}
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-50 ${
                tx.effective_classification === c
                  ? CLASSIFICATION_CHIP_CLASSES[c]
                  : "bg-slate-100 text-slate-700 hover:bg-slate-200"
              }`}
            >
              {CLASSIFICATION_LABELS[c]}
            </button>
          ),
        )}
        {tx.override_classification !== null ? (
          <button
            type="button"
            disabled={isPending}
            onClick={() => clearMutation.mutate()}
            className="rounded px-1.5 py-0.5 text-[10px] font-medium text-slate-600 hover:text-red-700 disabled:opacity-50"
            title="Revert to heuristic"
          >
            Clear
          </button>
        ) : null}
        <button
          type="button"
          onClick={onCloseEdit}
          className="rounded px-1.5 py-0.5 text-[10px] text-slate-500 hover:text-slate-900"
        >
          ✕
        </button>
      </div>
    );
  }

  const eff = tx.effective_classification;
  if (eff === null) {
    return (
      <button
        type="button"
        onClick={onStartEdit}
        className="text-[11px] text-slate-400 hover:text-slate-700"
        title="Not classified as a cashflow event (heuristic). Click to override."
      >
        —
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={onStartEdit}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium transition-opacity hover:opacity-80 ${CLASSIFICATION_CHIP_CLASSES[eff]}`}
      title={
        tx.override_classification !== null
          ? "User override — click to change or revert"
          : "Heuristic classification — click to override"
      }
    >
      {CLASSIFICATION_LABELS[eff]}
      {tx.override_classification !== null ? (
        <span className="text-[8px] uppercase opacity-70">override</span>
      ) : null}
    </button>
  );
}

