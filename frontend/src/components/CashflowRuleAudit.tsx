import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import {
  CLASSIFICATION_CHIP_CLASSES,
  CLASSIFICATION_LABELS,
  ErrorBanner,
} from "@/components/ui";
import type { CashflowRuleGroupOut, TxClassification } from "@/types";

/**
 * "Is my contributions-vs-gains tagging right?" in about a dozen decisions.
 *
 * The transactions list can answer the question but not efficiently: several
 * hundred classified rows, each needing the reviewer to recall the rule that
 * produced it. Those rows aren't independent though — they're a handful of
 * rules firing repeatedly, so the reviewable unit is the rule.
 *
 * Two choices carry the design:
 *
 *  - **Rank by dollars, not rows.** A rule firing 89 times for $50 a piece
 *    matters less than one firing once for $27,606, and count-ordering buries
 *    exactly the row worth finding.
 *  - **Split zero-impact rules out.** Roughly half the groups are dividends and
 *    reinvestments correctly marked Internal, contributing $0 by construction.
 *    Listing them alongside the ones that move the number triples the apparent
 *    workload, so they collapse behind a disclosure — present for the reviewer
 *    who wants to confirm nothing real is hiding in there, absent otherwise.
 *
 * Re-tagging writes through the same per-transaction override endpoint the
 * table uses, applied across the group's ids, so a rule is fixed once rather
 * than row by row.
 */

const SOURCE_LABEL: Record<CashflowRuleGroupOut["decision_source"], string> = {
  override: "you set it",
  name: "description word",
  sign: "amount sign",
  subtype: "subtype",
};

// Only `sign` gets a warning treatment. It's the one rule with no corroborating
// evidence — no override, no direction word, just a +/- that Plaid and
// SnapTrade disagree about — so it is where a silent inversion would hide.
const SOURCE_CHIP: Record<CashflowRuleGroupOut["decision_source"], string> = {
  override: "bg-slate-100 text-slate-600",
  name: "bg-slate-100 text-slate-600",
  sign: "bg-amber-100 text-amber-800",
  subtype: "bg-slate-100 text-slate-600",
};

const CLASSIFICATIONS: TxClassification[] = [
  "external_in",
  "external_out",
  "internal",
];

function money(value: string): string {
  const n = Number(value);
  return `${n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString(undefined, {
    maximumFractionDigits: 0,
  })}`;
}

export function CashflowRuleAudit(): JSX.Element {
  const queryClient = useQueryClient();
  const [showZero, setShowZero] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["cashflow-rule-audit"],
    queryFn: () => api.cashflowRuleAudit(),
  });

  const retag = useMutation({
    mutationFn: async (input: {
      ids: string[];
      classification: TxClassification;
    }) => {
      await Promise.all(
        input.ids.map((id) =>
          api.setTransactionOverride({
            plaid_investment_transaction_id: id,
            classification: input.classification,
          }),
        ),
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cashflow-rule-audit"] });
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      setOpenKey(null);
    },
  });

  const { material, zero } = useMemo(() => {
    const groups = data?.groups ?? [];
    return {
      material: groups.filter((g) => Number(g.net_cashflow) !== 0),
      zero: groups.filter((g) => Number(g.net_cashflow) === 0),
    };
  }, [data]);

  if (isLoading) {
    return <div className="text-xs text-slate-500">Loading rule audit…</div>;
  }
  if (isError || !data) {
    return <ErrorBanner>Could not load the cashflow rule audit.</ErrorBanner>;
  }

  function renderGroup(g: CashflowRuleGroupOut) {
    const key = `${g.decision_source}|${g.classification}|${g.type}|${g.subtype}`;
    const isOpen = openKey === key;
    return (
      <li key={key} className="border-t border-slate-100 py-2 first:border-t-0">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span
            className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
              CLASSIFICATION_CHIP_CLASSES[g.classification]
            }`}
          >
            {CLASSIFICATION_LABELS[g.classification]}
          </span>
          <span className="font-mono text-sm font-semibold tabular-nums text-slate-900">
            {money(g.net_cashflow)}
          </span>
          <span className="text-xs text-slate-500">
            {g.count} row{g.count === 1 ? "" : "s"}
          </span>
          <span
            className={`rounded px-1.5 py-0.5 text-[11px] ${
              SOURCE_CHIP[g.decision_source]
            }`}
            title={g.reason}
          >
            via {SOURCE_LABEL[g.decision_source]}
          </span>
          <span className="font-mono text-[11px] text-slate-400">
            {g.type}/{g.subtype ?? "—"}
          </span>
          {!g.counts_toward_return && (
            <span
              className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500"
              title="This account has no holdings data, so it is excluded from the return calculation."
            >
              not in return
            </span>
          )}
          <button
            type="button"
            onClick={() => setOpenKey(isOpen ? null : key)}
            className="ml-auto rounded px-2 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-slate-100"
          >
            {isOpen ? "Cancel" : "Re-tag all"}
          </button>
        </div>

        <div className="mt-1 text-[11px] text-slate-500">
          {g.first_date} → {g.last_date} · {g.accounts.join(", ")}
          {g.distinct_patterns > g.sample_names.length &&
            ` · ${g.distinct_patterns} description patterns`}
        </div>
        <ul className="mt-1 space-y-0.5">
          {g.sample_names.map((s) => (
            <li key={s} className="truncate font-mono text-[11px] text-slate-400">
              {s}
            </li>
          ))}
        </ul>

        {isOpen && (
          <div className="mt-2 flex flex-wrap items-center gap-2 rounded bg-slate-50 p-2">
            <span className="text-[11px] text-slate-600">
              Re-tag all {g.count} row{g.count === 1 ? "" : "s"} as:
            </span>
            {CLASSIFICATIONS.filter((c) => c !== g.classification).map((c) => (
              <button
                key={c}
                type="button"
                disabled={retag.isPending}
                onClick={() =>
                  retag.mutate({
                    ids: g.transaction_ids,
                    classification: c,
                  })
                }
                className="rounded bg-slate-900 px-2 py-1 text-[11px] font-medium text-white hover:bg-slate-700 disabled:opacity-50"
              >
                {CLASSIFICATION_LABELS[c]}
              </button>
            ))}
            <span className="text-[11px] text-slate-500">
              Writes an override on each row; reversible from the table below.
            </span>
          </div>
        )}
      </li>
    );
  }

  return (
    <section className="rounded border border-slate-200 bg-white p-3">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Cashflow rule audit
          </h2>
          <p className="text-xs text-slate-500">
            Every classification, grouped by the rule that made it. Ranked by
            effect on your return — check the top few and you have checked
            almost everything.
          </p>
        </div>
        <div className="text-right">
          <div className="font-mono text-sm font-semibold tabular-nums text-slate-900">
            {money(data.net_external_cashflow_in)}
          </div>
          <div className="text-[11px] text-slate-500">
            net contributions removed from your return
          </div>
        </div>
      </header>

      <ul className="mt-2">{material.map(renderGroup)}</ul>

      {zero.length > 0 && (
        <div className="mt-2 border-t border-slate-100 pt-2">
          <button
            type="button"
            onClick={() => setShowZero((v) => !v)}
            className="text-[11px] font-medium text-slate-600 hover:text-slate-900"
          >
            {showZero ? "▾" : "▸"} {zero.length} rule
            {zero.length === 1 ? "" : "s"} with no effect on the return (
            {zero.reduce((n, g) => n + g.count, 0)} rows — dividends,
            reinvestments, internal moves)
          </button>
          {showZero && <ul className="mt-1">{zero.map(renderGroup)}</ul>}
        </div>
      )}

      {retag.isError && (
        <p className="mt-2 text-[11px] text-rose-600">
          Re-tagging failed. Nothing was changed for the rows that errored.
        </p>
      )}
    </section>
  );
}
