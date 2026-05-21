import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "@/api/client";
import {
  Card,
  ErrorBanner,
  InfoButton,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";

/**
 * Monthly CIO brief card on the Dashboard.
 *
 * "Generate this month" kicks off an Opus call (~30-60s) on the
 * backend. The brief is persisted by `YYYY-MM`; regenerating the same
 * month overwrites the previous row.
 *
 * The HTML body is rendered in a sandboxed iframe so its CSS doesn't
 * collide with the rest of the app. (The backend wraps everything in
 * a `<!DOCTYPE html>` document with its own stylesheet.)
 */
export function LatestBriefCard(): JSX.Element {
  const queryClient = useQueryClient();
  const [selectedBriefId, setSelectedBriefId] = useState<number | null>(null);
  const [genError, setGenError] = useState<string | null>(null);

  const briefList = useQuery({
    queryKey: ["cio-briefs"],
    queryFn: () => api.cioListBriefs(),
  });

  // Default-select the latest brief whenever the list changes and we
  // don't already have a selection (or the previous selection vanished).
  const effectiveSelectedId = useMemo(() => {
    const list = briefList.data ?? [];
    if (list.length === 0) return null;
    if (selectedBriefId !== null && list.some((b) => b.brief_id === selectedBriefId)) {
      return selectedBriefId;
    }
    return list[0].brief_id;
  }, [briefList.data, selectedBriefId]);

  const briefBody = useQuery({
    queryKey: ["cio-brief", effectiveSelectedId],
    queryFn: () =>
      effectiveSelectedId === null
        ? Promise.resolve(null)
        : api.cioGetBrief(effectiveSelectedId),
    enabled: effectiveSelectedId !== null,
  });

  const generate = useMutation({
    mutationFn: () => api.cioGenerateBrief(),
    onSuccess: (brief) => {
      queryClient.invalidateQueries({ queryKey: ["cio-briefs"] });
      queryClient.invalidateQueries({ queryKey: ["cio-brief"] });
      setSelectedBriefId(brief.brief_id);
      setGenError(null);
    },
    onError: (err) => {
      setGenError(err instanceof Error ? err.message : "Generation failed.");
    },
  });

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-900">
            Monthly CIO brief
            <InfoButton
              label="Monthly CIO brief"
              explainer={{
                definition:
                  "An LLM-written portfolio review covering Executive Summary, Holdings, Concentration & Human Capital, Decision Log Status, Market Regime, and Action Items. Generated on demand by Claude Opus.",
                interpretation:
                  "Use the brief as a once-a-month deep read — it pulls from the same facts feed as the chat but spends more reasoning effort. Regenerating the same month overwrites; older months stay in the dropdown. Takes 30–60s; the button shows progress.",
              }}
            />
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            One HTML memo per calendar month. Regeneration overwrites.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={effectiveSelectedId ?? ""}
            onChange={(e) =>
              setSelectedBriefId(e.target.value ? parseInt(e.target.value, 10) : null)
            }
            className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
            disabled={(briefList.data ?? []).length === 0}
          >
            <option value="">— no briefs yet —</option>
            {(briefList.data ?? []).map((b) => (
              <option key={b.brief_id} value={b.brief_id}>
                {b.period_yyyymm}
                {b.model_used ? ` · ${b.model_used}` : ""}
              </option>
            ))}
          </select>
          <PrimaryButton
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
          >
            {generate.isPending ? "Generating… (~45s)" : "Generate this month"}
          </PrimaryButton>
        </div>
      </header>

      {genError && (
        <div className="px-4 py-2">
          <ErrorBanner>{genError}</ErrorBanner>
        </div>
      )}

      <div className="px-4 py-3">
        {briefList.isLoading ? (
          <p className="text-xs text-slate-500">Loading briefs…</p>
        ) : effectiveSelectedId === null ? (
          <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
            No briefs generated yet. Click "Generate this month" — Opus
            takes ~30-60 seconds.
          </div>
        ) : briefBody.isLoading ? (
          <p className="text-xs text-slate-500">Loading brief…</p>
        ) : briefBody.data ? (
          <>
            <div className="mb-2 flex items-center gap-3 text-xs text-slate-500">
              <span className="font-mono">{briefBody.data.period_yyyymm}</span>
              <span>Generated {briefBody.data.generated_at.slice(0, 10)}</span>
              {briefBody.data.model_used && (
                <span className="text-slate-400">
                  · model: {briefBody.data.model_used}
                </span>
              )}
              <a
                href={`data:text/html;charset=utf-8,${encodeURIComponent(briefBody.data.html)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="ml-auto rounded border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-600 hover:bg-slate-100"
              >
                Open standalone ↗
              </a>
              <SecondaryButton
                onClick={() => {
                  if (
                    window.confirm(
                      `Regenerate ${briefBody.data?.period_yyyymm}? The current brief will be overwritten.`,
                    )
                  ) {
                    generate.mutate();
                  }
                }}
              >
                Regenerate
              </SecondaryButton>
            </div>
            <iframe
              title={`CIO brief ${briefBody.data.period_yyyymm}`}
              srcDoc={briefBody.data.html}
              className="w-full rounded-md border border-slate-200"
              style={{ height: "70vh" }}
              sandbox="allow-same-origin"
            />
          </>
        ) : null}
      </div>
    </Card>
  );
}
