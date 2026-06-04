import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "@/api/client";
import {
  Card,
  DangerLink,
  EmptyState,
  ErrorBanner,
  InfoBanner,
  PageHeader,
  Panel,
  Pill,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";
import type { ExecutionStatus, QueueItem } from "@/types";

/**
 * The decision cockpit — the action queue.
 *
 * Reads the persisted, Opus-ranked queue (`/api/cockpit/queue`) and lets you
 * accept / snooze / dismiss each item. "Refresh" regenerates from the latest
 * holdings + theses + valuations; the queue is grounded (every item cites real
 * numbers) and advisory (an explicit suggested action, never a command). Thesis
 * health and valuation are weighted above concentration.
 */

const TIER_TONE = { act: "loss", watch: "warn", fine: "muted" } as const;
const TIER_LABEL = { act: "Act now", watch: "Watch", fine: "Fine" } as const;

// Reconciliation: how the accepted trade actually played out.
const EXEC_TONE = {
  pending: "muted",
  executed: "gain",
  partial: "warn",
  not_executed: "loss",
  "n/a": "muted",
} as const;
const EXEC_LABEL = {
  pending: "Awaiting trade",
  executed: "Executed",
  partial: "Partial",
  not_executed: "Not executed",
  "n/a": "No trade",
} as const;

export function Cockpit(): JSX.Element {
  const qc = useQueryClient();
  const [useLlm, setUseLlm] = useState(true);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["cockpit-queue"],
    queryFn: () => api.cockpitQueue(),
  });

  const refresh = useMutation({
    mutationFn: () => api.cockpitRefresh(useLlm),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cockpit-queue"] }),
  });

  const items = data ?? [];
  const actCount = items.filter((i) => i.tier === "act").length;
  const watchCount = items.filter((i) => i.tier === "watch").length;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Cockpit"
        eyebrow="Decision queue"
        actions={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <input
                type="checkbox"
                checked={!useLlm}
                onChange={(e) => setUseLlm(!e.target.checked)}
              />
              Fast (skip Opus)
            </label>
            <PrimaryButton onClick={() => refresh.mutate()} disabled={refresh.isPending}>
              {refresh.isPending ? "Refreshing…" : "Refresh"}
            </PrimaryButton>
          </div>
        }
      />

      {refresh.isPending && useLlm && (
        <InfoBanner>
          Asking Opus to rank the grounded signals — this can take ~10–30s.
        </InfoBanner>
      )}
      {refresh.isError && (
        <ErrorBanner>Refresh failed: {(refresh.error as Error).message}</ErrorBanner>
      )}

      {isLoading ? (
        <Card className="px-4 py-3 text-sm text-muted">Loading the action queue…</Card>
      ) : isError ? (
        <ErrorBanner>Failed to load the action queue.</ErrorBanner>
      ) : items.length === 0 ? (
        <EmptyState>
          Nothing in the queue. Hit <span className="font-medium">Refresh</span> to regenerate
          from the latest holdings, theses, and valuations.
        </EmptyState>
      ) : (
        <Panel
          eyebrow={`${items.length} open`}
          title="What needs me"
          sub={`${actCount} to act · ${watchCount} to watch · thesis & valuation weighted above concentration`}
          bodyClassName="divide-y divide-hairline"
        >
          {items.map((item) => (
            <QueueRow key={item.id} item={item} />
          ))}
        </Panel>
      )}
    </div>
  );
}

function QueueRow({ item }: { item: QueueItem }): JSX.Element {
  const qc = useQueryClient();
  const invalidate = (): void => {
    void qc.invalidateQueries({ queryKey: ["cockpit-queue"] });
  };
  const accept = useMutation({ mutationFn: () => api.cockpitAccept(item.id), onSuccess: invalidate });
  const snooze = useMutation({
    mutationFn: () => api.cockpitSnooze(item.id, 7),
    onSuccess: invalidate,
  });
  const dismiss = useMutation({
    mutationFn: () => api.cockpitDismiss(item.id),
    onSuccess: invalidate,
  });
  const busy = accept.isPending || snooze.isPending || dismiss.isPending;
  const accepted = item.status === "accepted";
  const evidence = Object.entries(item.evidence);

  return (
    <div className="px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Pill tone={TIER_TONE[item.tier]}>{TIER_LABEL[item.tier]}</Pill>
        <span className="font-mono text-sm font-semibold text-ink">{item.ticker}</span>
        {item.name && <span className="truncate text-muted">{item.name}</span>}
        <Pill tone="neutral">{item.action}</Pill>
        {item.weight_pct > 0 && <Pill tone="muted">{item.weight_pct.toFixed(1)}% of book</Pill>}
        {!item.llm_ranked && (
          <Pill tone="muted" title="Deterministic signal — not LLM-ranked">
            rule
          </Pill>
        )}
        {accepted && <Pill tone="gain">✓ accepted</Pill>}
      </div>

      <p className="mt-1.5 text-sm font-medium leading-snug text-ink">{item.headline}</p>
      <p className="mt-1 text-xs leading-relaxed text-body">{item.rationale}</p>
      <p className="mt-1.5 text-xs text-ink-soft">
        <span className="font-semibold">Suggested: </span>
        {item.suggested_action}
      </p>

      {evidence.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {evidence.map(([k, v]) => (
            <span
              key={k}
              className="rounded bg-paper px-1.5 py-0.5 font-mono text-[10px] text-muted"
            >
              {k}: {v}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex items-center gap-3">
        {!accepted && (
          <>
            <PrimaryButton onClick={() => accept.mutate()} disabled={busy}>
              Accept
            </PrimaryButton>
            <SecondaryButton onClick={() => snooze.mutate()} disabled={busy}>
              Snooze 7d
            </SecondaryButton>
            <DangerLink onClick={() => dismiss.mutate()}>Dismiss</DangerLink>
          </>
        )}
        <Link
          to={`/advisor?ask=${encodeURIComponent(`Why ${item.action} ${item.ticker}? ${item.headline}`)}`}
          className="text-xs text-accent hover:underline"
        >
          Discuss
        </Link>
      </div>

      {accepted && <ExecutionPanel item={item} />}
    </div>
  );
}

/**
 * For an accepted item, shows how the trade actually reconciled (auto-matched
 * on each refresh) and lets the owner correct it. A manual correction sticks —
 * the daily reconciler skips overridden rows.
 */
function ExecutionPanel({ item }: { item: QueueItem }): JSX.Element {
  const qc = useQueryClient();
  const setExec = useMutation({
    mutationFn: (status: string) => api.cockpitSetExecution(item.id, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cockpit-queue"] }),
  });
  const status: ExecutionStatus = item.execution_status;

  return (
    <div className="mt-2.5 rounded-md bg-paper px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-muted">Execution</span>
        <Pill tone={EXEC_TONE[status]}>{EXEC_LABEL[status]}</Pill>
        {item.execution_overridden && (
          <Pill tone="muted" title="Manually set — the daily reconciler won't change it">
            manual
          </Pill>
        )}
        {item.executed_weight_pct != null && (
          <span className="font-mono text-[10px] text-muted">
            now {item.executed_weight_pct.toFixed(1)}%
          </span>
        )}
      </div>

      {item.execution_notes && (
        <p className="mt-1 text-[11px] leading-relaxed text-muted">{item.execution_notes}</p>
      )}

      <div className="mt-1.5 flex flex-wrap items-center gap-3 text-[11px]">
        <span className="text-muted">Correct:</span>
        <button
          type="button"
          disabled={setExec.isPending}
          onClick={() => setExec.mutate("executed")}
          className="text-accent hover:underline disabled:opacity-50"
        >
          Executed
        </button>
        <button
          type="button"
          disabled={setExec.isPending}
          onClick={() => setExec.mutate("not_executed")}
          className="text-accent hover:underline disabled:opacity-50"
        >
          Not executed
        </button>
        <button
          type="button"
          disabled={setExec.isPending}
          onClick={() => setExec.mutate("pending")}
          className="text-muted hover:underline disabled:opacity-50"
        >
          Reset
        </button>
      </div>
    </div>
  );
}
