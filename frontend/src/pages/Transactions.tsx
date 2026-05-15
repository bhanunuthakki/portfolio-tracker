import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import { Card, EmptyState, ErrorBanner, Td, Th } from "@/components/ui";
import type { InvestmentTransactionOut, TxClassification } from "@/types";

/**
 * Transactions table with inline cashflow-classification overrides.
 *
 * Each row's "Counted as" column shows what the TWR / contributions pipeline
 * does with that row today. A small popover lets the user force a different
 * classification (writes to `transaction_overrides`). Useful when an ACATS
 * move shows up as a "contribution" in one feed but is really an internal
 * transfer between two of your own accounts.
 *
 * Filters at the top let the user narrow to just transfers/cash events (where
 * the override matters) so the table isn't a 500-row wall.
 */

type Filter = "all" | "cashflow" | "overridden";

const FILTER_LABELS: Record<Filter, string> = {
  all: "All",
  cashflow: "Cashflow rows",
  overridden: "Overridden only",
};

const CLASSIFICATION_LABELS: Record<TxClassification, string> = {
  external_in: "Contribution",
  external_out: "Withdrawal",
  internal: "Internal",
};

const CLASSIFICATION_CHIP_CLASSES: Record<TxClassification, string> = {
  external_in: "bg-emerald-100 text-emerald-800",
  external_out: "bg-rose-100 text-rose-800",
  internal: "bg-slate-200 text-slate-700",
};

export function Transactions(): JSX.Element {
  const [filter, setFilter] = useState<Filter>("all");
  const [editingId, setEditingId] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["transactions"],
    queryFn: () => api.transactions({ limit: 500 }),
  });

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

  const filtered = data.filter((t) => {
    if (filter === "all") return true;
    if (filter === "overridden") return t.override_classification !== null;
    if (filter === "cashflow") return t.effective_classification !== null;
    return true;
  });

  const overriddenCount = data.filter(
    (t) => t.override_classification !== null,
  ).length;

  return (
    <div className="space-y-3">
      <header className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-xs text-slate-500">
          {data.length} transactions loaded · {overriddenCount} overridden
        </div>
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
      </header>

      <Card className="overflow-hidden">
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <Th>Date</Th>
              <Th>Account</Th>
              <Th>Type</Th>
              <Th>Ticker</Th>
              <Th>Description</Th>
              <Th align="right">Quantity</Th>
              <Th align="right">Amount</Th>
              <Th>Counted as</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filtered.map((t) => (
              <TxRow
                key={t.plaid_investment_transaction_id}
                tx={t}
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
        <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
          Showing {filtered.length} of {data.length}. Click the &ldquo;Counted as&rdquo;
          chip to override how the TWR / contributions pipeline treats a row.
          Overrides write to `transaction_overrides` and apply to every window.
        </p>
      </Card>
    </div>
  );
}

function TxRow({
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
  return (
    <tr className="hover:bg-slate-50">
      <Td className="tabular-nums text-slate-900">{tx.date}</Td>
      <Td>{tx.account_name}</Td>
      <Td>
        <span className="inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-xs uppercase tracking-wide text-slate-700">
          {tx.subtype ?? tx.type}
        </span>
      </Td>
      <Td className="font-mono text-slate-900">{tx.ticker ?? "—"}</Td>
      <Td className="text-slate-600">
        <span className="block max-w-[280px] truncate" title={tx.name ?? ""}>
          {tx.name ?? "—"}
        </span>
      </Td>
      <Td align="right" className="tabular-nums">
        {parseFloat(tx.quantity).toLocaleString(undefined, {
          maximumFractionDigits: 4,
        })}
      </Td>
      <Td align="right" className="tabular-nums">
        ${parseFloat(tx.amount).toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}
      </Td>
      <Td>
        <ClassificationCell
          tx={tx}
          isEditing={isEditing}
          onStartEdit={onStartEdit}
          onCloseEdit={onCloseEdit}
        />
      </Td>
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
      queryClient.invalidateQueries({ queryKey: ["performance"] });
      queryClient.invalidateQueries({ queryKey: ["cashflow-audit"] });
      onCloseEdit();
    },
  });
  const clearMutation = useMutation({
    mutationFn: () =>
      api.deleteTransactionOverride(tx.plaid_investment_transaction_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["performance"] });
      queryClient.invalidateQueries({ queryKey: ["cashflow-audit"] });
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
    // Not a cashflow event (buy/sell/dividend); show muted "—" but still
    // let the user override if they really want to.
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
