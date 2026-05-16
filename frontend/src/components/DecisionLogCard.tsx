import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import {
  Card,
  DangerLink,
  ErrorBanner,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";
import type { DecisionIn, DecisionOut } from "@/types";

/**
 * Pre-trade decision log.
 *
 * The whole feature is a thin wrapper around `POST /api/decisions`. The
 * point isn't the database — it's the form. Filling it out before each
 * trade is the friction that kills low-conviction ideas. The list view
 * below is a journal of past decisions you can review against actual
 * outcomes once `attachOutcome` is fired weeks/months later.
 *
 * Action vocabulary is constrained to a small set so it's mineable —
 * "buy/add" tells you about openings, "sell/trim" about exits, "hold"
 * is for the every-quarter-review pattern.
 */
export function DecisionLogCard(): JSX.Element {
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const decisions = useQuery({
    queryKey: ["decisions"],
    queryFn: () => api.listDecisions({ limit: 50 }),
  });

  return (
    <Card>
      <header className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Pre-trade decision log
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Write the thesis BEFORE the trade. The friction is the feature.
          </p>
        </div>
        <PrimaryButton onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Close" : "+ Log decision"}
        </PrimaryButton>
      </header>

      {showForm && (
        <DecisionForm
          onCancel={() => {
            setShowForm(false);
            setError(null);
          }}
          onSubmitted={() => {
            setShowForm(false);
            setError(null);
            queryClient.invalidateQueries({ queryKey: ["decisions"] });
          }}
          onError={setError}
        />
      )}

      {error && (
        <div className="px-4 py-2">
          <ErrorBanner>{error}</ErrorBanner>
        </div>
      )}

      <DecisionList
        decisions={decisions.data ?? []}
        isLoading={decisions.isLoading}
        onChanged={() => queryClient.invalidateQueries({ queryKey: ["decisions"] })}
      />
    </Card>
  );
}

const ACTION_OPTIONS = ["buy", "add", "trim", "sell", "hold"] as const;
const CONFIDENCE_OPTIONS = ["low", "medium", "high"] as const;

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function DecisionForm({
  onCancel,
  onSubmitted,
  onError,
}: {
  onCancel: () => void;
  onSubmitted: () => void;
  onError: (msg: string) => void;
}): JSX.Element {
  const [form, setForm] = useState<DecisionIn>({
    decision_date: todayISO(),
    ticker: "",
    action: "buy",
    thesis: "",
    expected_outcome: "",
    invalidation_triggers: "",
    position_size_pct: undefined,
    time_horizon_months: undefined,
    confidence: "medium",
    notes: "",
    linked_brief_path: "",
  });

  const create = useMutation({
    mutationFn: (payload: DecisionIn) => api.createDecision(payload),
    onSuccess: onSubmitted,
    onError: (err) =>
      onError(err instanceof Error ? err.message : "Failed to log decision"),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.ticker.trim()) return onError("Ticker is required");
    if (form.thesis.trim().length < 10)
      return onError("Thesis must be at least 10 characters");
    create.mutate({
      ...form,
      ticker: form.ticker.toUpperCase().trim(),
      thesis: form.thesis.trim(),
      expected_outcome: form.expected_outcome?.trim() || null,
      invalidation_triggers: form.invalidation_triggers?.trim() || null,
      notes: form.notes?.trim() || null,
      linked_brief_path: form.linked_brief_path?.trim() || null,
    });
  };

  return (
    <form
      onSubmit={submit}
      className="grid gap-3 border-b border-slate-100 bg-slate-50 px-4 py-3"
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="Date">
          <input
            type="date"
            value={form.decision_date}
            onChange={(e) =>
              setForm((f) => ({ ...f, decision_date: e.target.value }))
            }
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </Field>
        <Field label="Ticker">
          <input
            type="text"
            value={form.ticker}
            onChange={(e) =>
              setForm((f) => ({ ...f, ticker: e.target.value.toUpperCase() }))
            }
            placeholder="NVDA"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm font-mono uppercase"
          />
        </Field>
        <Field label="Action">
          <select
            value={form.action}
            onChange={(e) =>
              setForm((f) => ({ ...f, action: e.target.value }))
            }
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          >
            {ACTION_OPTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Confidence">
          <select
            value={form.confidence ?? "medium"}
            onChange={(e) =>
              setForm((f) => ({ ...f, confidence: e.target.value }))
            }
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          >
            {CONFIDENCE_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label="Thesis (1-2 paragraphs)">
        <textarea
          value={form.thesis}
          onChange={(e) => setForm((f) => ({ ...f, thesis: e.target.value }))}
          rows={4}
          placeholder="What does the company do? Why is the market mispricing it? What specific catalyst do you expect to drive a re-rating?"
          className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm leading-relaxed"
        />
      </Field>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Expected outcome">
          <textarea
            value={form.expected_outcome ?? ""}
            onChange={(e) =>
              setForm((f) => ({ ...f, expected_outcome: e.target.value }))
            }
            rows={2}
            placeholder="e.g., Re-rating to $250 over 12 months. P/E expansion from 22x to 28x."
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="Invalidation triggers">
          <textarea
            value={form.invalidation_triggers ?? ""}
            onChange={(e) =>
              setForm((f) => ({ ...f, invalidation_triggers: e.target.value }))
            }
            rows={2}
            placeholder="e.g., Q3 revenue growth < 25%; gross margin contracts > 200 bps. (Specific facts, NOT just 'stock fell.')"
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Field label="Target size (% of portfolio)">
          <input
            type="number"
            step="0.5"
            min={0}
            max={100}
            value={form.position_size_pct ?? ""}
            onChange={(e) =>
              setForm((f) => ({
                ...f,
                position_size_pct: e.target.value
                  ? parseFloat(e.target.value)
                  : undefined,
              }))
            }
            placeholder="3.0"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </Field>
        <Field label="Time horizon (months)">
          <input
            type="number"
            min={0}
            max={120}
            value={form.time_horizon_months ?? ""}
            onChange={(e) =>
              setForm((f) => ({
                ...f,
                time_horizon_months: e.target.value
                  ? parseInt(e.target.value, 10)
                  : undefined,
              }))
            }
            placeholder="12"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm tabular-nums"
          />
        </Field>
        <Field label="Notes (optional)">
          <input
            type="text"
            value={form.notes ?? ""}
            onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))}
            placeholder="anything else"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </Field>
      </div>

      <Field label="Linked brief (optional)">
        <BriefPicker
          ticker={form.ticker}
          value={form.linked_brief_path ?? ""}
          onChange={(v) => setForm((f) => ({ ...f, linked_brief_path: v }))}
        />
      </Field>

      <div className="flex items-center justify-end gap-2">
        <SecondaryButton onClick={onCancel}>Cancel</SecondaryButton>
        <PrimaryButton type="submit" disabled={create.isPending}>
          {create.isPending ? "Logging…" : "Log decision"}
        </PrimaryButton>
      </div>
    </form>
  );
}

function BriefPicker({
  ticker,
  value,
  onChange,
}: {
  ticker: string;
  value: string;
  onChange: (v: string) => void;
}): JSX.Element {
  const tickerNorm = ticker.toUpperCase().trim();
  const { data } = useQuery({
    queryKey: ["briefs-for-ticker", tickerNorm],
    queryFn: () => api.listBriefsForTicker(tickerNorm),
    enabled: tickerNorm.length > 0,
  });
  const briefs = data?.briefs ?? [];
  return (
    <div className="space-y-1">
      {briefs.length > 0 ? (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded border border-slate-300 px-2 py-1 text-xs font-mono"
        >
          <option value="">— no brief —</option>
          {briefs.map((b) => (
            <option key={b.path} value={b.path}>
              {b.iso_date}
            </option>
          ))}
        </select>
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            tickerNorm
              ? `No briefs found for ${tickerNorm}. Paste a path if you have one.`
              : "Enter a ticker above to see available briefs"
          }
          className="w-full rounded border border-slate-300 px-2 py-1 text-xs font-mono"
        />
      )}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      <span className="font-medium uppercase tracking-wider text-[10px] text-slate-500">
        {label}
      </span>
      {children}
    </label>
  );
}

function DecisionList({
  decisions,
  isLoading,
  onChanged,
}: {
  decisions: DecisionOut[];
  isLoading: boolean;
  onChanged: () => void;
}): JSX.Element {
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteDecision(id),
    onSuccess: onChanged,
  });

  if (isLoading) {
    return <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>;
  }
  if (decisions.length === 0) {
    return (
      <div className="px-4 py-6 text-xs text-slate-500">
        No decisions logged yet. Log one before your next trade.
      </div>
    );
  }
  return (
    <ul className="divide-y divide-slate-100">
      {decisions.map((d) => (
        <li key={d.decision_id} className="px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-xs text-slate-500">
                <span className="font-mono text-slate-700">{d.ticker}</span>
                <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] uppercase text-slate-700">
                  {d.action}
                </span>
                {d.confidence && (
                  <span
                    className={[
                      "rounded px-1.5 py-0.5 text-[10px] uppercase",
                      d.confidence === "high"
                        ? "bg-emerald-100 text-emerald-800"
                        : d.confidence === "low"
                          ? "bg-amber-100 text-amber-800"
                          : "bg-slate-200 text-slate-700",
                    ].join(" ")}
                  >
                    {d.confidence}
                  </span>
                )}
                {d.position_size_pct && (
                  <span className="text-slate-500 tabular-nums">
                    {parseFloat(d.position_size_pct).toFixed(1)}%
                  </span>
                )}
                <span className="ml-auto tabular-nums">{d.decision_date}</span>
              </div>
              <p className="mt-1.5 text-sm text-slate-800 leading-relaxed">
                {d.thesis}
              </p>
              {(d.expected_outcome || d.invalidation_triggers) && (
                <div className="mt-2 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                  {d.expected_outcome && (
                    <div>
                      <span className="font-medium text-slate-700">
                        Expected:{" "}
                      </span>
                      <span className="text-slate-600">
                        {d.expected_outcome}
                      </span>
                    </div>
                  )}
                  {d.invalidation_triggers && (
                    <div>
                      <span className="font-medium text-slate-700">
                        Invalidates if:{" "}
                      </span>
                      <span className="text-slate-600">
                        {d.invalidation_triggers}
                      </span>
                    </div>
                  )}
                </div>
              )}
              {d.linked_brief_path && (
                <div className="mt-2 text-xs">
                  <a
                    href={`/api/earnings-summary/brief/${encodeURIComponent(d.ticker)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded bg-indigo-100 px-1.5 py-0.5 font-medium text-indigo-800 hover:bg-indigo-200"
                    title={d.linked_brief_path}
                  >
                    📄 brief at decision time
                  </a>
                </div>
              )}
              {d.matched_executions && (
                <div className="mt-2 text-xs">
                  <span
                    className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-800 ring-1 ring-emerald-200"
                    title={`Detected by auto-match within 14d of decision_date. ${d.matched_executions.first_date} → ${d.matched_executions.last_date}`}
                  >
                    ✓ Acted —{" "}
                    {d.matched_executions.transaction_count}{" "}
                    {d.matched_executions.direction}
                    {d.matched_executions.transaction_count === 1 ? "" : "s"} ·{" "}
                    {parseFloat(d.matched_executions.total_quantity).toLocaleString(
                      undefined,
                      { maximumFractionDigits: 4 },
                    )}{" "}
                    sh · $
                    {Math.abs(
                      parseFloat(d.matched_executions.total_amount),
                    ).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  </span>
                </div>
              )}
              {d.outcome_status && (
                <div className="mt-2 text-xs">
                  <span
                    className={[
                      "rounded px-1.5 py-0.5 text-[10px] uppercase",
                      d.outcome_status === "validated"
                        ? "bg-emerald-100 text-emerald-800"
                        : d.outcome_status === "invalidated"
                          ? "bg-red-100 text-red-800"
                          : "bg-slate-200 text-slate-700",
                    ].join(" ")}
                  >
                    {d.outcome_status}
                  </span>
                  {d.outcome_notes && (
                    <span className="ml-2 text-slate-600">{d.outcome_notes}</span>
                  )}
                </div>
              )}
            </div>
            <DangerLink onClick={() => remove.mutate(d.decision_id)}>
              Delete
            </DangerLink>
          </div>
        </li>
      ))}
    </ul>
  );
}
