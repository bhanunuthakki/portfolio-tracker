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
  bucket: string;
  weight_pct: string; // string for intermediate typing values
  notes: string;
  // True for rows the user just added and hasn't saved yet. We bundle save
  // by ticker, so the "Save" button on a row commits ALL its sibling rows.
  isDirty?: boolean;
}

/**
 * Edit the human-capital overlap buckets per ticker.
 *
 * Each ticker carries a vector of (bucket, weight%) rows. The coaching
 * engine sums portfolio-wide weighted exposure per bucket and fires
 * `concentration_human_capital_aggregate` when it exceeds the bucket
 * cap shown in the header. Bucket caps are configured in
 * `services/coaching.py` — edit there + restart API to change them.
 *
 * Save semantics: the API uses PUT-by-ticker (the body is the full set
 * of bucket rows for that ticker), so saving one ticker doesn't touch
 * the others. The "Save" button gathers all rows for the ticker on
 * that line and PUTs them in one call.
 */
export function HumanCapitalEditor(): JSX.Element {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["human-capital-overlaps"],
    queryFn: () => api.listHumanCapitalOverlaps(),
  });

  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (data) {
      const flat: Row[] = [];
      for (const o of data.overlaps) {
        for (const b of o.buckets) {
          flat.push({
            ticker: o.ticker,
            bucket: b.bucket,
            weight_pct: b.weight_pct,
            notes: b.notes ?? "",
          });
        }
      }
      flat.sort((a, b) =>
        a.ticker === b.ticker
          ? a.bucket.localeCompare(b.bucket)
          : a.ticker.localeCompare(b.ticker),
      );
      setRows(flat);
    }
  }, [data]);

  const saveTicker = useMutation({
    mutationFn: ({
      ticker,
      tickerRows,
    }: {
      ticker: string;
      tickerRows: Row[];
    }) =>
      api.upsertHumanCapitalOverlap({
        ticker,
        buckets: tickerRows
          .filter((r) => r.bucket.trim())
          .map((r) => ({
            bucket: r.bucket.trim(),
            weight_pct: parseFloat(r.weight_pct) || 0,
            notes: r.notes.trim() || null,
          })),
      }),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["human-capital-overlaps"] });
      // Coaching engine reads bucket weights on every /tips call; bust
      // its cache so aggregate tips refresh after edits.
      queryClient.invalidateQueries({ queryKey: ["coaching-tips"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Save failed"),
  });

  const deleteTicker = useMutation({
    mutationFn: (ticker: string) => api.deleteHumanCapitalOverlap(ticker),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["human-capital-overlaps"] });
      queryClient.invalidateQueries({ queryKey: ["coaching-tips"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Delete failed"),
  });

  const updateRow = (idx: number, patch: Partial<Row>) => {
    setRows((prev) =>
      prev.map((r, i) => (i === idx ? { ...r, ...patch, isDirty: true } : r)),
    );
  };
  const addRow = () =>
    setRows((prev) => [
      ...prev,
      { ticker: "", bucket: "", weight_pct: "100", notes: "", isDirty: true },
    ]);
  const removeRow = (idx: number) =>
    setRows((prev) => prev.filter((_, i) => i !== idx));

  const saveTickerFromRow = (rowIdx: number) => {
    const ticker = rows[rowIdx]?.ticker.trim().toUpperCase();
    if (!ticker) {
      setError("Ticker is required");
      return;
    }
    const tickerRows = rows.filter(
      (r) => r.ticker.trim().toUpperCase() === ticker,
    );
    saveTicker.mutate({ ticker, tickerRows });
  };

  const caps = data?.bucket_caps_pct ?? {};
  const knownBuckets = Object.keys(caps).sort();

  return (
    <Card className="overflow-hidden">
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">
            Human-capital overlap buckets
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Per-ticker weighted exposure to each human-capital risk bucket.
            The coaching engine flags portfolio-wide aggregate exposure that
            exceeds the cap shown below for any single bucket.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-600">
          {knownBuckets.map((b) => (
            <span
              key={b}
              className="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 tabular-nums"
              title={`Aggregate cap for ${b}`}
            >
              <span className="font-mono">{b}</span>{" "}
              <span className="font-medium text-slate-900">
                ≤ {parseFloat(caps[b]).toFixed(0)}%
              </span>
            </span>
          ))}
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
                <Th>Bucket</Th>
                <Th align="right">Weight (%)</Th>
                <Th>Notes</Th>
                <Th align="right">Actions</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((r, idx) => (
                <tr key={idx} className={r.isDirty ? "bg-amber-50/40" : ""}>
                  <Td>
                    <input
                      type="text"
                      value={r.ticker}
                      onChange={(e) =>
                        updateRow(idx, { ticker: e.target.value.toUpperCase() })
                      }
                      placeholder="META"
                      className="w-24 rounded border border-slate-300 px-2 py-1 text-sm font-mono uppercase tabular-nums"
                    />
                  </Td>
                  <Td>
                    <input
                      type="text"
                      value={r.bucket}
                      onChange={(e) => updateRow(idx, { bucket: e.target.value })}
                      placeholder="big_tech_ads"
                      list="hc-known-buckets"
                      className="w-44 rounded border border-slate-300 px-2 py-1 text-sm font-mono"
                    />
                  </Td>
                  <Td align="right">
                    <input
                      type="number"
                      step="5"
                      min="0"
                      max="100"
                      value={r.weight_pct}
                      onChange={(e) =>
                        updateRow(idx, { weight_pct: e.target.value })
                      }
                      className="w-20 rounded border border-slate-300 px-2 py-1 text-right text-sm tabular-nums"
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
                    <div className="flex items-center justify-end gap-2">
                      <SecondaryButton
                        onClick={() => saveTickerFromRow(idx)}
                        disabled={saveTicker.isPending}
                      >
                        Save {r.ticker || "row"}
                      </SecondaryButton>
                      <DangerLink onClick={() => removeRow(idx)}>
                        Remove
                      </DangerLink>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>

          <datalist id="hc-known-buckets">
            {knownBuckets.map((b) => (
              <option key={b} value={b} />
            ))}
          </datalist>

          {error && (
            <div className="px-4 py-2">
              <ErrorBanner>{error}</ErrorBanner>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 bg-slate-50 px-4 py-3">
            <SecondaryButton onClick={addRow}>+ Add row</SecondaryButton>
            <PrimaryButton
              onClick={() => {
                const tickers = Array.from(
                  new Set(
                    rows
                      .filter((r) => r.ticker.trim() && r.isDirty)
                      .map((r) => r.ticker.trim().toUpperCase()),
                  ),
                );
                if (tickers.length === 0) {
                  setError("No unsaved rows");
                  return;
                }
                for (const t of tickers) {
                  saveTicker.mutate({
                    ticker: t,
                    tickerRows: rows.filter(
                      (r) => r.ticker.trim().toUpperCase() === t,
                    ),
                  });
                }
              }}
              disabled={saveTicker.isPending}
            >
              Save all dirty
            </PrimaryButton>
            <DangerLink
              onClick={() => {
                const t = prompt("Ticker to delete all bucket rows for:")
                  ?.trim()
                  .toUpperCase();
                if (t) deleteTicker.mutate(t);
              }}
            >
              Delete ticker…
            </DangerLink>
            <span className="ml-auto text-xs text-slate-500">
              PUT replaces all bucket rows for the saved ticker.
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
