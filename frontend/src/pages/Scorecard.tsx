import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Panel,
  Pill,
  SortableTh,
  Stat,
  Td,
} from "@/components/ui";
import { useTableSort } from "@/components/useTableSort";
import type { DecisionOut } from "@/types";

/**
 * Decision scorecard — were you right for the *right reason*?
 *
 * Reads the decision log (/api/decisions) and grades it on PROCESS: the
 * outcome you logged against each pre-trade thesis + invalidation triggers.
 * Realized price action is context, not the score (the grill: grade process,
 * show outcome alongside). Closed-position attribution lives on the Trade
 * analysis / Trade timeline pages; P6 groups all three under "History".
 */

type Tone = "gain" | "loss" | "warn" | "muted";

function outcomeTone(status: string | null): Tone {
  if (!status) return "muted";
  const s = status.toLowerCase();
  if (s.includes("invalid") || s.includes("broke") || s.includes("wrong")) return "loss";
  if (s.includes("valid") || s.includes("right") || s.includes("confirm")) return "gain";
  if (s.includes("partial") || s.includes("mixed")) return "warn";
  return "muted";
}

function short(text: string | null, n = 160): string {
  if (!text) return "—";
  const t = text.trim();
  return t.length <= n ? t : `${t.slice(0, n - 1)}…`;
}

export function Scorecard(): JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["decisions-all"],
    queryFn: () => api.listDecisions(),
  });
  const rows = data ?? [];
  const withOutcome = rows.filter((d) => d.outcome_status).length;
  const withTriggers = rows.filter((d) => d.invalidation_triggers).length;

  const sort = useTableSort<DecisionOut>(rows, "decision_date", "desc", {
    decision_date: (d) => d.decision_date,
    ticker: (d) => d.ticker,
    action: (d) => d.action,
    thesis: (d) => d.thesis,
    outcome: (d) => d.outcome_status ?? "",
    horizon: (d) => d.time_horizon_months,
  });

  return (
    <div className="space-y-5">
      <PageHeader title="Decision scorecard" eyebrow="Process" />
      {isLoading ? (
        <Card className="px-4 py-3 text-sm text-muted">Loading the decision log…</Card>
      ) : isError ? (
        <ErrorBanner>Failed to load the decision log.</ErrorBanner>
      ) : rows.length === 0 ? (
        <EmptyState>
          No decisions logged yet. Log a pre-trade thesis (with invalidation triggers) in the
          decision log, then grade it here as outcomes land.
        </EmptyState>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Decisions" value={String(rows.length)} />
            <Stat label="Outcomes graded" value={`${withOutcome}/${rows.length}`} />
            <Stat label="With invalidation triggers" value={`${withTriggers}/${rows.length}`} />
            <Stat label="Still open" value={String(rows.length - withOutcome)} />
          </div>
          <Panel
            eyebrow="Were you right for the right reason?"
            title="Decision log, graded"
            sub="The outcome you logged is the process grade; realized price action is context, not the score."
            bodyClassName="overflow-x-auto"
          >
            <table className="min-w-full divide-y divide-hairline">
              <thead>
                <tr>
                  <SortableTh column="decision_date" sort={sort}>
                    Date
                  </SortableTh>
                  <SortableTh column="ticker" sort={sort}>
                    Ticker
                  </SortableTh>
                  <SortableTh column="action" sort={sort}>
                    Action
                  </SortableTh>
                  <SortableTh column="thesis" sort={sort}>
                    Thesis / invalidation triggers
                  </SortableTh>
                  <SortableTh column="outcome" sort={sort}>
                    Outcome
                  </SortableTh>
                  <SortableTh column="horizon" align="right" sort={sort}>
                    Horizon
                  </SortableTh>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {sort.sortedRows.map((d) => (
                  <ScorecardRow key={d.decision_id} d={d} />
                ))}
              </tbody>
            </table>
          </Panel>
        </>
      )}
    </div>
  );
}

function ScorecardRow({ d }: { d: DecisionOut }): JSX.Element {
  return (
    <tr className="align-top">
      <Td className="whitespace-nowrap font-mono text-xs">{d.decision_date}</Td>
      <Td className="font-mono text-sm font-semibold text-ink">{d.ticker}</Td>
      <Td>
        <Pill tone="neutral">{d.action}</Pill>
      </Td>
      <Td>
        <div className="max-w-md text-xs leading-snug text-body">{short(d.thesis)}</div>
        {d.invalidation_triggers && (
          <div className="mt-1 max-w-md text-xs leading-snug text-muted">
            <span className="font-semibold">Invalidation: </span>
            {short(d.invalidation_triggers)}
          </div>
        )}
        {d.linked_brief_path && (
          <span className="mt-1 inline-block text-[10px] text-faint">brief linked</span>
        )}
      </Td>
      <Td>
        {d.outcome_status ? (
          <div className="space-y-1">
            <Pill tone={outcomeTone(d.outcome_status)}>{d.outcome_status}</Pill>
            {d.outcome_notes && (
              <div className="max-w-xs text-xs leading-snug text-muted">
                {short(d.outcome_notes, 120)}
              </div>
            )}
            {d.outcome_date && <div className="text-[10px] text-faint">{d.outcome_date}</div>}
          </div>
        ) : (
          <Pill tone="muted">open</Pill>
        )}
      </Td>
      <Td align="right" className="font-mono text-xs tabular-nums">
        {d.time_horizon_months != null ? `${d.time_horizon_months}mo` : "—"}
      </Td>
    </tr>
  );
}
