import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/api/client";
import type { MonthlyBriefJobOut } from "@/types";
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
 * The Opus call backing "Generate this month" takes ~30-60s. Rather
 * than holding an HTTP request open for the full duration (and losing
 * progress on a page reload), the backend returns a job_id immediately
 * (HTTP 202) and runs generation in a BackgroundTask. This component
 * polls `/briefs/jobs/{job_id}` every 3 seconds for status.
 *
 * On mount, we ask the backend for any pending/running jobs and resume
 * polling — so refreshing the page mid-generation doesn't strand the
 * user without a progress indicator.
 *
 * The HTML body is rendered in a sandboxed iframe so its CSS doesn't
 * collide with the rest of the app. (The backend wraps everything in
 * a `<!DOCTYPE html>` document with its own stylesheet.)
 */

const POLL_INTERVAL_MS = 3000;
const EXPECTED_RANGE_LABEL = "typically 30-60s";

export function LatestBriefCard(): JSX.Element {
  const queryClient = useQueryClient();
  const [selectedBriefId, setSelectedBriefId] = useState<number | null>(null);
  const [genError, setGenError] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState<number | null>(null);
  // Bumped every second while a job is polling so the elapsed-time
  // counter ticks forward without depending on poll cadence.
  const [tickNow, setTickNow] = useState(() => Date.now());

  const briefList = useQuery({
    queryKey: ["cio-briefs"],
    queryFn: () => api.cioListBriefs(),
  });

  // One-shot on mount: detect any in-flight job (e.g. the user reloaded
  // the page while a previous generation was still running) and resume
  // polling it.
  //
  // Query key uses a `cio-job-*` namespace rather than `cio-brief-*` so
  // it does NOT match the prefix-based invalidations of `["cio-brief"]`
  // / `["cio-briefs"]` below — without this isolation, a successful
  // brief invalidates the in-flight query, which refetches and triggers
  // the success effect again, looping until the browser exhausts its
  // connection pool.
  const inFlightOnMount = useQuery({
    queryKey: ["cio-jobs-in-flight"],
    queryFn: () => api.cioListBriefJobs(["pending", "running"]),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  // Latched so the resume-on-mount effect fires *once*. Without this,
  // when the job completes and we set activeJobId=null, this effect
  // would re-run (its `activeJobId` dep just changed), see the stale
  // `inFlightOnMount.data` row, and immediately resume polling the
  // just-completed job — an infinite loop of poll+invalidate that
  // exhausts the browser's connection pool.
  const mountResumeLatchedRef = useRef(false);
  useEffect(() => {
    if (mountResumeLatchedRef.current) return;
    if (activeJobId !== null) return;
    const jobs = inFlightOnMount.data;
    if (jobs === undefined) return; // still loading
    mountResumeLatchedRef.current = true;
    if (jobs.length > 0) {
      // Server returns newest first; pick the most recent in-flight job.
      setActiveJobId(jobs[0].job_id);
    }
  }, [inFlightOnMount.data, activeJobId]);

  // Active job poll. Stops automatically once status flips to a terminal
  // value (returning `false` from `refetchInterval`).
  // `refetchIntervalInBackground: true` keeps polling alive when the
  // browser tab is unfocused — without it, generation that finishes
  // while the user is in another tab leaves the UI stuck on
  // "Generating…" until they click back in.
  // Uses `cio-job-status` namespace (not `cio-brief-job-*`) for the
  // same prefix-isolation reason described on `cio-jobs-in-flight`.
  const jobStatus = useQuery({
    queryKey: ["cio-job-status", activeJobId],
    queryFn: () =>
      activeJobId === null
        ? Promise.resolve(null)
        : api.cioGetBriefJob(activeJobId),
    enabled: activeJobId !== null,
    refetchInterval: (query) => {
      const data = query.state.data as MonthlyBriefJobOut | null | undefined;
      if (data?.status === "succeeded" || data?.status === "failed") {
        return false;
      }
      return POLL_INTERVAL_MS;
    },
    refetchIntervalInBackground: true,
  });

  // React to terminal status flips: pull the freshly-generated brief
  // into the dropdown, or surface the failure.
  useEffect(() => {
    const job = jobStatus.data;
    if (!job) return;
    if (job.status === "succeeded" && job.brief_id !== null) {
      setSelectedBriefId(job.brief_id);
      // `exact: true` so we don't accidentally invalidate query keys
      // that *prefix-match* "cio-brief" (e.g. an in-flight job poll) —
      // re-fetching those would re-fire this effect, looping until the
      // browser runs out of connection slots.
      queryClient.invalidateQueries({
        queryKey: ["cio-briefs"],
        exact: true,
      });
      // Bust the brief-by-id cache too — when regenerating an existing
      // month, brief_id is unchanged (UPSERT) but the html body just
      // changed, so the iframe must re-render with the new srcDoc.
      queryClient.invalidateQueries({
        queryKey: ["cio-brief", job.brief_id],
        exact: true,
      });
      setActiveJobId(null);
      setGenError(null);
    } else if (job.status === "failed") {
      setGenError(job.error ?? "Generation failed.");
      setActiveJobId(null);
    }
  }, [jobStatus.data, queryClient]);

  // Drive the elapsed-time counter while a job is active.
  useEffect(() => {
    if (activeJobId === null) return;
    const id = window.setInterval(() => setTickNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [activeJobId]);

  const isJobActive =
    activeJobId !== null &&
    jobStatus.data?.status !== "succeeded" &&
    jobStatus.data?.status !== "failed";

  const elapsedSec = useMemo(() => {
    if (!isJobActive || !jobStatus.data) return null;
    const started = Date.parse(jobStatus.data.started_at);
    return Math.max(0, Math.floor((tickNow - started) / 1000));
  }, [isJobActive, jobStatus.data, tickNow]);

  const effectiveSelectedId = useMemo(() => {
    const list = briefList.data ?? [];
    if (list.length === 0) return null;
    if (
      selectedBriefId !== null &&
      list.some((b) => b.brief_id === selectedBriefId)
    ) {
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
    onSuccess: (job) => {
      setGenError(null);
      // Whether this is a freshly-inserted pending job or an existing
      // in-flight one (returned idempotently when a regen is already
      // running), we just start polling its id.
      setActiveJobId(job.job_id);
    },
    onError: (err) => {
      setGenError(err instanceof Error ? err.message : "Generation failed.");
    },
  });

  const generateButtonLabel = isJobActive
    ? elapsedSec !== null
      ? `Generating… ${elapsedSec}s (${EXPECTED_RANGE_LABEL})`
      : "Generating…"
    : "Generate this month";

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
                  "Use the brief as a once-a-month deep read — it pulls from the same facts feed as the chat but spends more reasoning effort. Regenerating the same month overwrites; older months stay in the dropdown. Takes 30-60s; generation runs in the background so the page stays interactive and progress survives a page reload.",
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
              setSelectedBriefId(
                e.target.value ? parseInt(e.target.value, 10) : null,
              )
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
            disabled={generate.isPending || isJobActive}
          >
            {generateButtonLabel}
          </PrimaryButton>
        </div>
      </header>

      {isJobActive && jobStatus.data && (
        <div className="border-b border-slate-100 px-4 py-2 text-xs text-slate-600">
          <div className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-amber-500" />
            <span>
              Generating brief for{" "}
              <span className="font-mono">{jobStatus.data.period_yyyymm}</span>
              {elapsedSec !== null && ` · ${elapsedSec}s elapsed`} ·{" "}
              <span className="text-slate-400">{EXPECTED_RANGE_LABEL}</span>
            </span>
          </div>
          {/* Indeterminate-style progress bar: fill grows linearly up to
              60s, then caps at 90% so it doesn't claim to be done before
              the model actually returns. */}
          <div className="mt-1.5 h-1 w-full overflow-hidden rounded bg-slate-100">
            <div
              className="h-full rounded bg-amber-400 transition-all"
              style={{
                width:
                  elapsedSec === null
                    ? "0%"
                    : `${Math.min(90, (elapsedSec / 60) * 90)}%`,
              }}
            />
          </div>
        </div>
      )}

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
                disabled={isJobActive}
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
