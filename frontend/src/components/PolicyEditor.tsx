import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/api/client";
import {
  Card,
  DangerLink,
  ErrorBanner,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";

interface Row {
  ticker: string;
  weight_pct: string; // string so the user can type intermediate values
  notes: string;
}

/**
 * Edit the user's policy-portfolio weights.
 *
 * Stored in basis points server-side; surfaced as percent for usability.
 * Total must sum to ~100% to be considered "balanced" (the API flags it
 * but doesn't reject sub/over-balanced policies — useful when the user
 * wants to model leverage or holding cash).
 */
export function PolicyEditor(): JSX.Element {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["policy"],
    queryFn: () => api.getPolicy(),
  });

  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Sync local edit state when the server response loads / changes.
  useEffect(() => {
    if (data) {
      setRows(
        data.weights.map((w) => ({
          ticker: w.ticker,
          weight_pct: w.weight_pct,
          notes: w.notes ?? "",
        })),
      );
    }
  }, [data]);

  const save = useMutation({
    mutationFn: (next: Row[]) =>
      api.putPolicy(
        next
          .filter((r) => r.ticker.trim())
          .map((r) => ({
            ticker: r.ticker.trim().toUpperCase(),
            weight_pct: parseFloat(r.weight_pct) || 0,
            notes: r.notes.trim() || null,
          })),
      ),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["policy"] });
      // Performance + beta queries depend on policy weights for the
      // synthetic POLICY benchmark — invalidate them too.
      queryClient.invalidateQueries({ queryKey: ["performance"] });
      queryClient.invalidateQueries({ queryKey: ["beta"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Save failed"),
  });

  const total = rows.reduce((s, r) => s + (parseFloat(r.weight_pct) || 0), 0);
  const isBalanced = Math.abs(total - 100) < 0.01;

  const updateRow = (idx: number, patch: Partial<Row>) => {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };
  const addRow = () =>
    setRows((prev) => [...prev, { ticker: "", weight_pct: "0", notes: "" }]);
  const removeRow = (idx: number) =>
    setRows((prev) => prev.filter((_, i) => i !== idx));

  return (
    <Card className="overflow-hidden">
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Policy benchmark weights
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Defines the synthetic <em>Policy</em> line in the Dashboard chart
            and the <em>vs Policy</em> regression in the Risk Metrics card.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span
            className={[
              "tabular-nums font-medium",
              isBalanced ? "text-emerald-700" : "text-amber-700",
            ].join(" ")}
          >
            Total: {total.toFixed(2)}%
            {!isBalanced && total !== 0 && " (unbalanced)"}
          </span>
        </div>
      </header>

      {isLoading ? (
        <div className="px-4 py-6 text-xs text-slate-500">Loading…</div>
      ) : (
        <>
          <table className="min-w-full divide-y divide-slate-200">
            <thead className="bg-slate-50">
              <tr>
                <Th>Ticker</Th>
                <Th align="right">Weight (%)</Th>
                <Th>Notes</Th>
                <Th align="right">Action</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((r, idx) => (
                <tr key={idx}>
                  <Td>
                    <input
                      type="text"
                      value={r.ticker}
                      onChange={(e) => updateRow(idx, { ticker: e.target.value })}
                      placeholder="QQQ"
                      className="w-28 rounded border border-slate-300 px-2 py-1 text-sm font-mono uppercase tabular-nums"
                    />
                  </Td>
                  <Td align="right">
                    <input
                      type="number"
                      step="0.5"
                      min="0"
                      max="100"
                      value={r.weight_pct}
                      onChange={(e) =>
                        updateRow(idx, { weight_pct: e.target.value })
                      }
                      className="w-24 rounded border border-slate-300 px-2 py-1 text-right text-sm tabular-nums"
                    />
                  </Td>
                  <Td>
                    <input
                      type="text"
                      value={r.notes}
                      onChange={(e) => updateRow(idx, { notes: e.target.value })}
                      placeholder="optional"
                      className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                    />
                  </Td>
                  <Td align="right">
                    <DangerLink onClick={() => removeRow(idx)}>Remove</DangerLink>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>

          {error && (
            <div className="px-4 py-2">
              <ErrorBanner>{error}</ErrorBanner>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 bg-slate-50 px-4 py-3">
            <SecondaryButton onClick={addRow}>+ Add ticker</SecondaryButton>
            <PrimaryButton
              onClick={() => save.mutate(rows)}
              disabled={save.isPending}
            >
              Save policy
            </PrimaryButton>
            <span className="ml-auto text-xs text-slate-500">
              After saving a new ticker, run{" "}
              <code className="bg-white border border-slate-200 px-1 rounded">
                python -m portfolio_tracker.jobs.benchmarks --start 2024-01-01
              </code>{" "}
              to fetch its price history.
            </span>
          </div>
        </>
      )}
    </Card>
  );
}

function Th({
  children,
  align,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}): JSX.Element {
  return (
    <th
      scope="col"
      className={[
        "px-4 py-2 text-xs font-medium uppercase tracking-wide text-slate-500",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}): JSX.Element {
  return (
    <td
      className={[
        "px-4 py-2 text-sm text-slate-700",
        align === "right" ? "text-right" : "text-left",
      ].join(" ")}
    >
      {children}
    </td>
  );
}
