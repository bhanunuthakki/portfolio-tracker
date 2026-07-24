import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import { DataQualityReport } from "@/components/DataQualityReport";
import { PositioningSection } from "@/components/PositioningSection";
import {
  Card,
  EmptyState,
  ErrorBanner,
  fmtSignedUSD,
  fmtUSD,
  pnlClass,
  SortableTh,
  Td,
} from "@/components/ui";
import { useTableSort } from "@/components/useTableSort";
import type {
  ConsolidatedHoldingOut,
  CostBasisSource,
  HoldingByAccountOut,
} from "@/types";

const SOURCE_LABEL: Record<CostBasisSource, string> = {
  manual: "manual",
  inferred_acats: "ACATS inferred",
  inferred_1099: "1099 inferred",
};

const SOURCE_CLASS: Record<CostBasisSource, string> = {
  manual: "bg-amber-100 text-amber-800",
  inferred_acats: "bg-sky-100 text-sky-800",
  inferred_1099: "bg-indigo-100 text-indigo-800",
};

export function Holdings(): JSX.Element {
  const [searchParams] = useSearchParams();
  // Deep-link support: /holdings?ticker=NU highlights + scrolls to that row
  // (the research command center can link here). Absent → no focused row.
  const focusTicker = (searchParams.get("ticker") ?? "").toUpperCase();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["holdings"],
    queryFn: () => api.latestHoldings(),
  });

  const accessors = useMemo(
    () => ({
      ticker: (h: ConsolidatedHoldingOut) => h.ticker ?? "",
      name: (h: ConsolidatedHoldingOut) => h.name ?? "",
      quantity: (h: ConsolidatedHoldingOut) => parseFloat(h.total_quantity),
      avg_cost: (h: ConsolidatedHoldingOut) =>
        h.weighted_avg_cost_per_share !== null
          ? parseFloat(h.weighted_avg_cost_per_share)
          : null,
      cost: (h: ConsolidatedHoldingOut) =>
        h.total_cost_basis !== null ? parseFloat(h.total_cost_basis) : null,
      value: (h: ConsolidatedHoldingOut) =>
        h.total_value !== null ? parseFloat(h.total_value) : null,
      unrealized: (h: ConsolidatedHoldingOut) =>
        h.unrealized_pnl !== null ? parseFloat(h.unrealized_pnl) : null,
      accounts: (h: ConsolidatedHoldingOut) => h.accounts.length,
    }),
    [],
  );
  // Default to largest position first — the natural way to read a book.
  const sort = useTableSort(data ?? [], "value", "desc", accessors);

  if (isLoading) return <div className="text-sm text-slate-500">Loading…</div>;
  if (isError)
    return <ErrorBanner>Failed to load holdings.</ErrorBanner>;
  if (!data || data.length === 0) {
    return (
      <EmptyState>
        No holdings yet. Link an account, then run the snapshotter.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-4">
      <DataQualityReport />
      <PositioningSection />
      <Card className="overflow-hidden">
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <SortableTh column="ticker" sort={sort}>Ticker</SortableTh>
              <SortableTh column="name" sort={sort}>Name</SortableTh>
              <SortableTh column="quantity" align="right" sort={sort}>
                Quantity
              </SortableTh>
              <SortableTh column="avg_cost" align="right" sort={sort}>
                Avg cost / share
              </SortableTh>
              <SortableTh column="cost" align="right" sort={sort}>
                Cost basis
              </SortableTh>
              <SortableTh column="value" align="right" sort={sort}>
                Value
              </SortableTh>
              <SortableTh column="unrealized" align="right" sort={sort}>
                Unrealized
              </SortableTh>
              <SortableTh column="accounts" align="right" sort={sort}>
                Accounts
              </SortableTh>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sort.sortedRows.map((h) => (
              <Row
                key={h.security_id}
                h={h}
                focus={focusTicker !== "" && (h.ticker ?? "").toUpperCase() === focusTicker}
              />
            ))}
          </tbody>
        </table>
        <p className="border-t border-slate-100 bg-slate-50 px-4 py-2.5 text-xs text-slate-500">
          Positions consolidated by ticker. Click any header to sort; click any
          row to expand the per-account drill-down. Avg cost = total cost basis ÷
          total shares.
        </p>
      </Card>
    </div>
  );
}

function Row({ h, focus }: { h: ConsolidatedHoldingOut; focus: boolean }): JSX.Element {
  const [open, setOpen] = useState(focus); // auto-expand the deep-linked row
  const rowRef = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    if (focus && rowRef.current) {
      rowRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [focus]);
  const value = h.total_value !== null ? parseFloat(h.total_value) : null;
  const cost = h.total_cost_basis !== null ? parseFloat(h.total_cost_basis) : null;
  const unrealized = h.unrealized_pnl !== null ? parseFloat(h.unrealized_pnl) : null;
  const avgCost =
    h.weighted_avg_cost_per_share !== null
      ? parseFloat(h.weighted_avg_cost_per_share)
      : null;
  const accountCount = h.accounts.length;
  // Aggregate per-account cost basis sources for the top-row badge.
  const sources = new Set(
    h.accounts.map((a) => a.cost_basis_source).filter(Boolean) as CostBasisSource[],
  );
  // Pick the most specific badge: prefer inferred over manual when both
  // are present (inferred is the more interesting case).
  const headlineSource: CostBasisSource | null =
    sources.has("inferred_acats")
      ? "inferred_acats"
      : sources.has("inferred_1099")
        ? "inferred_1099"
        : sources.has("manual")
          ? "manual"
          : null;

  return (
    <>
      <tr
        ref={rowRef}
        className={`cursor-pointer hover:bg-slate-50 ${focus ? "bg-amber-100" : ""}`}
        onClick={() => setOpen(!open)}
      >
        <Td className="font-mono text-slate-900">
          <span className="inline-block w-3 text-slate-400">
            {open ? "▾" : "▸"}
          </span>{" "}
          {h.ticker ?? "—"}
        </Td>
        <Td className="text-slate-600">{h.name ?? "—"}</Td>
        <Td align="right" className="tabular-nums">
          {parseFloat(h.total_quantity).toLocaleString(undefined, {
            maximumFractionDigits: 4,
          })}
        </Td>
        <Td align="right" className="tabular-nums">
          {avgCost !== null ? `$${Math.round(avgCost).toLocaleString()}` : "—"}
        </Td>
        <Td align="right" className="tabular-nums">
          <span className="inline-flex items-center gap-1">
            {fmtUSD(cost)}
            {headlineSource ? (
              <span
                className={`rounded px-1 py-0.5 text-[9px] uppercase tracking-wide ${SOURCE_CLASS[headlineSource]}`}
                title={`Cost basis is ${SOURCE_LABEL[headlineSource]} for at least one account (not broker-reported). Click the row to see per-account detail.`}
              >
                {headlineSource === "manual" ? "MAN" : "INF"}
              </span>
            ) : null}
            {h.has_unreliable_cost_basis ? (
              <span
                className="rounded bg-rose-100 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-rose-800"
                title="Broker-reported cost basis looks implausibly low vs market value for one or more accounts. Treat unrealized P&L on this row as unreliable. Click to see which account, then set an override."
              >
                UNREL
              </span>
            ) : null}
          </span>
        </Td>
        <Td
          align="right"
          className="tabular-nums font-medium text-slate-900"
        >
          {fmtUSD(value)}
        </Td>
        <Td
          align="right"
          className={[
            "tabular-nums",
            h.has_unreliable_cost_basis ? "text-slate-400 italic" : pnlClass(unrealized),
          ].join(" ")}
          title={
            h.has_unreliable_cost_basis
              ? "Unreliable — one or more accounts have implausible cost basis"
              : undefined
          }
        >
          {fmtSignedUSD(unrealized)}
        </Td>
        <Td align="right" className="text-slate-500">
          {accountCount} {accountCount === 1 ? "account" : "accounts"}
        </Td>
      </tr>
      {open && (
        <tr className="bg-slate-50/60">
          <td colSpan={8} className="px-4 py-3 cursor-default" onClick={(e) => e.stopPropagation()}>
            <table className="min-w-full text-xs">
              <thead className="text-slate-500 uppercase tracking-wide">
                <tr>
                  <th className="pb-1 text-left font-medium">Account</th>
                  <th className="pb-1 text-right font-medium">Quantity</th>
                  <th className="pb-1 text-right font-medium">Cost basis</th>
                  <th className="pb-1 text-right font-medium">Value</th>
                  <th className="pb-1 text-right font-medium">% of position</th>
                  <th className="pb-1 text-right font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {h.accounts.map((a) => (
                  <AccountRow
                    key={a.account_id}
                    a={a}
                    securityId={h.security_id}
                    totalValue={value}
                  />
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}

function AccountRow({
  a,
  securityId,
  totalValue,
}: {
  a: HoldingByAccountOut;
  securityId: number;
  totalValue: number | null;
}): JSX.Element {
  const [editing, setEditing] = useState(false);
  const aValue = a.institution_value !== null ? parseFloat(a.institution_value) : null;
  const aCost = a.cost_basis !== null ? parseFloat(a.cost_basis) : null;
  const pct =
    aValue !== null && totalValue !== null && totalValue > 0
      ? (aValue / totalValue) * 100
      : null;
  const needsAttention = a.cost_basis_unreliable || aCost === null;

  return (
    <>
      <tr>
        <td className="py-1 text-slate-700">{a.account_name}</td>
        <td className="py-1 text-right tabular-nums">
          {parseFloat(a.quantity).toLocaleString(undefined, { maximumFractionDigits: 4 })}
        </td>
        <td className="py-1 text-right tabular-nums">
          <span className="inline-flex items-center gap-1">
            <span className={a.cost_basis_unreliable ? "text-slate-400 italic" : ""}>
              {fmtUSD(aCost)}
            </span>
            {a.cost_basis_source ? (
              <span
                className={`rounded px-1 py-0.5 text-[9px] uppercase tracking-wide ${SOURCE_CLASS[a.cost_basis_source]}`}
                title={`Source: ${SOURCE_LABEL[a.cost_basis_source]}. Not broker-reported.`}
              >
                {SOURCE_LABEL[a.cost_basis_source]}
              </span>
            ) : null}
            {a.cost_basis_unreliable ? (
              <span
                className="rounded bg-rose-100 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-rose-800"
                title="Broker-reported cost basis looks implausibly low vs market value."
              >
                UNREL
              </span>
            ) : null}
          </span>
        </td>
        <td className="py-1 text-right tabular-nums">{fmtUSD(aValue)}</td>
        <td className="py-1 text-right tabular-nums text-slate-500">
          {pct !== null ? `${pct.toFixed(0)}%` : "—"}
        </td>
        <td className="py-1 text-right">
          <button
            type="button"
            onClick={() => setEditing((e) => !e)}
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
              needsAttention
                ? "bg-amber-100 text-amber-800 hover:bg-amber-200"
                : "text-slate-500 hover:bg-slate-200"
            }`}
            title="Set / override the cost basis for this account-position. Will be tagged 'manual'."
          >
            {editing ? "Cancel" : a.cost_basis_source === "manual" ? "Edit" : "Fix"}
          </button>
        </td>
      </tr>
      {editing && (
        <tr>
          <td colSpan={6} className="py-2">
            <CostBasisEditor
              accountId={a.account_id}
              securityId={securityId}
              accountName={a.account_name}
              quantity={parseFloat(a.quantity)}
              currentValue={aValue}
              currentCost={aCost}
              existingOverride={a.cost_basis_source === "manual" || a.cost_basis_source === "inferred_acats"}
              onDone={() => setEditing(false)}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function CostBasisEditor({
  accountId,
  securityId,
  accountName,
  quantity,
  currentValue,
  currentCost,
  existingOverride,
  onDone,
}: {
  accountId: number;
  securityId: number;
  accountName: string;
  quantity: number;
  currentValue: number | null;
  currentCost: number | null;
  existingOverride: boolean;
  onDone: () => void;
}): JSX.Element {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(
    existingOverride && currentCost !== null
      ? String(Math.round(currentCost))
      : "",
  );
  const [acquiredAt, setAcquiredAt] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (totalCost: number) =>
      api.setCostBasisOverride({
        account_id: accountId,
        security_id: securityId,
        total_cost_basis: totalCost,
        notes: notes.trim() || null,
        acquired_at: acquiredAt.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
      onDone();
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Save failed"),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteCostBasisOverride(accountId, securityId),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["data-quality"] });
      onDone();
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Delete failed"),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const n = parseFloat(value);
    if (Number.isNaN(n) || n < 0) {
      setError("Enter a non-negative dollar amount");
      return;
    }
    save.mutate(n);
  };

  const perShare =
    value && quantity > 0 && parseFloat(value) > 0
      ? `≈ $${Math.round(parseFloat(value) / quantity).toLocaleString()}/share`
      : "";

  return (
    <form
      onSubmit={onSubmit}
      className="rounded border border-slate-200 bg-white p-3 space-y-2"
    >
      <div className="text-xs text-slate-700">
        Setting cost basis for <strong>{accountName}</strong> · {quantity.toFixed(4)} sh
        {currentValue !== null && (
          <> · current value <strong>${currentValue.toLocaleString(undefined, { maximumFractionDigits: 0 })}</strong></>
        )}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-xs text-slate-600">
          Total cost basis ($)
          <input
            type="number"
            step="0.01"
            min="0"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. 12500"
            autoFocus
            className="mt-1 w-36 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </label>
        <label
          className="flex flex-col text-xs text-slate-600"
          title="Date the shares were originally acquired (e.g., ACATS source-broker purchase date). Used as the SPY counterfactual anchor in Trade Timeline + Position Alpha. Leave blank for unknown."
        >
          Acquired on (optional)
          <input
            type="date"
            value={acquiredAt}
            onChange={(e) => setAcquiredAt(e.target.value)}
            placeholder="blank = unknown (uses earliest snapshot date as proxy)"
            className="mt-1 w-40 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </label>
        <label className="flex flex-col text-xs text-slate-600 flex-1 min-w-[160px]">
          Note (optional)
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="e.g. computed from 1099-B"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <button
          type="submit"
          disabled={save.isPending}
          className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : "Save (manual)"}
        </button>
        {existingOverride && (
          <button
            type="button"
            onClick={() => remove.mutate()}
            disabled={remove.isPending}
            className="rounded border border-red-200 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            title="Remove the override, fall back to broker-reported cost basis"
          >
            Clear override
          </button>
        )}
        {perShare && (
          <span className="text-xs text-slate-500 self-center">{perShare}</span>
        )}
      </div>
      {error && <div className="text-xs text-red-700">{error}</div>}
    </form>
  );
}
