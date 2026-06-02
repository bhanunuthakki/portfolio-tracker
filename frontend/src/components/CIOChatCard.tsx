import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import {
  Card,
  DangerLink,
  ErrorBanner,
  InfoButton,
  PrimaryButton,
  SecondaryButton,
} from "@/components/ui";
import type { ChatSessionOut, ChatTurnOut } from "@/types";

/**
 * CIO advisor chat card on the Dashboard.
 *
 * Sessions ("threads") are per-conversation; switching threads loads
 * their transcript. The first turn auto-attaches a facts block; later
 * turns re-attach on `/refresh` or after 24h. The backend persists
 * everything — refreshing the page restores the active thread.
 *
 * The Claude call streams token-by-token via SSE
 * (`/sessions/{id}/turns/stream`). While in-flight, two locally-rendered
 * "pending" bubbles sit at the bottom of the transcript: the user's
 * outgoing message (already persisted server-side) and the assistant
 * reply (filled in chunk-by-chunk). Once the stream's terminal `done`
 * event arrives we invalidate the turns query and the pending bubbles
 * are replaced by the persisted rows.
 */
export function CIOChatCard(): JSX.Element {
  // `?ask=<question>` lets other surfaces (e.g. a cockpit item's "Discuss")
  // deep-link in with the question pre-filled. Seed the draft once on mount.
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [newThreadTitle, setNewThreadTitle] = useState("");
  const [draftMessage, setDraftMessage] = useState(() => searchParams.get("ask") ?? "");
  const [sendError, setSendError] = useState<string | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);

  // In-flight stream state. While `pendingUser` is non-null the send
  // button shows "Streaming…" and the textarea is disabled. The two
  // bubbles render below the persisted transcript so the user sees them
  // immediately rather than waiting for the round-trip.
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const [pendingAssistant, setPendingAssistant] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  // AbortController so a session switch / unmount cuts the in-flight
  // fetch. The backend kills its subprocess on disconnect.
  const abortRef = useRef<AbortController | null>(null);

  const sessions = useQuery({
    queryKey: ["cio-sessions"],
    queryFn: () => api.cioListSessions(),
  });

  // Auto-select the first session (most-recently-updated) when the list
  // first arrives, so the card has something to show out of the box.
  useEffect(() => {
    if (
      activeSessionId === null &&
      sessions.data &&
      sessions.data.length > 0
    ) {
      setActiveSessionId(sessions.data[0].session_id);
    }
  }, [sessions.data, activeSessionId]);

  const turns = useQuery({
    queryKey: ["cio-turns", activeSessionId],
    queryFn: () =>
      activeSessionId === null
        ? Promise.resolve([])
        : api.cioListTurns(activeSessionId),
    enabled: activeSessionId !== null,
  });

  const visibleTurns = useMemo(
    () => (turns.data ?? []).filter((t) => t.role !== "system"),
    [turns.data],
  );

  // Auto-scroll the transcript to the bottom when a new turn arrives
  // OR a new token chunk arrives during streaming.
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [visibleTurns.length, pendingUser, pendingAssistant]);

  const createSession = useMutation({
    mutationFn: (title: string) =>
      api.cioCreateSession({ title: title.trim() || null }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["cio-sessions"] });
      setActiveSessionId(session.session_id);
      setNewThreadTitle("");
    },
  });

  const deleteSession = useMutation({
    mutationFn: (sessionId: number) => api.cioDeleteSession(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cio-sessions"] });
      setActiveSessionId(null);
    },
  });

  const handleSend = async (): Promise<void> => {
    if (activeSessionId === null) {
      setSendError("Open a thread first.");
      return;
    }
    const content = draftMessage.trim();
    if (!content) return;
    if (streaming) return;

    setSendError(null);
    setStreaming(true);
    setPendingUser(content);
    setPendingAssistant("");
    setDraftMessage("");
    // Snapshot the session id so a switch mid-stream doesn't misroute
    // the cache invalidation when the stream finishes.
    const sessionId = activeSessionId;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;

    let accumulated = "";
    let lastError: string | null = null;

    try {
      for await (const ev of api.cioStreamTurn(
        sessionId,
        { content },
        controller.signal,
      )) {
        if ("text" in ev) {
          accumulated += ev.text;
          setPendingAssistant(accumulated);
        } else if ("error" in ev) {
          lastError = ev.error;
        } else if ("done" in ev && ev.done) {
          // Server already persisted both turns. Drop the pending
          // bubbles and refetch so the canonical rows render with
          // their real ids/timestamps.
          break;
        }
        // user_turn_id event is ignored — invalidation will refetch it.
      }
    } catch (err) {
      // AbortError is expected on session switch / unmount; suppress.
      const isAbort =
        err instanceof DOMException && err.name === "AbortError";
      if (!isAbort) {
        lastError =
          err instanceof Error ? err.message : "Stream failed unexpectedly.";
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
      setStreaming(false);
      setPendingUser(null);
      setPendingAssistant(null);
      if (lastError !== null) {
        setSendError(lastError);
      }
      queryClient.invalidateQueries({ queryKey: ["cio-turns", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["cio-sessions"] });
    }
  };

  // Abort the in-flight stream on unmount (component leaves the DOM).
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  return (
    <Card>
      <header className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-900">
            CIO advisor
            <InfoButton
              label="CIO advisor"
              explainer={{
                definition:
                  "LLM-powered portfolio coaching. Each thread captures a snapshot of your holdings, decision log, and the rule-based coaching engine's findings, then asks Claude (Sonnet) to contextualize them against the persona + market.",
                interpretation:
                  "Use it to think through specific decisions ('trim NU?'), stress-test theses against macro, or pressure-check the rule-based flags. Type /refresh to force a re-snapshot if you've just synced. Threads persist; pick one from the dropdown or start a new one.",
              }}
            />
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Ask anything about your portfolio. Threads remember context.
          </p>
        </div>
      </header>

      <div className="flex flex-col gap-2 border-b border-slate-100 bg-slate-50 px-4 py-3 sm:flex-row sm:items-center">
        <label className="flex flex-1 items-center gap-2 text-xs text-slate-600">
          Active thread:
          <select
            value={activeSessionId ?? ""}
            onChange={(e) =>
              setActiveSessionId(e.target.value ? parseInt(e.target.value, 10) : null)
            }
            className="flex-1 rounded border border-slate-300 bg-white px-2 py-1 text-sm"
          >
            <option value="">— select —</option>
            {(sessions.data ?? []).map((s: ChatSessionOut) => (
              <option key={s.session_id} value={s.session_id}>
                {s.title} · {s.turn_count} turn{s.turn_count === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>
        {activeSessionId !== null && (
          <DangerLink
            onClick={() => {
              if (
                window.confirm(
                  "Delete this thread? The transcript is permanently lost.",
                )
              ) {
                deleteSession.mutate(activeSessionId);
              }
            }}
          >
            Delete thread
          </DangerLink>
        )}
      </div>

      <div className="flex flex-col gap-2 border-b border-slate-100 bg-slate-50/60 px-4 py-2.5 sm:flex-row sm:items-center">
        <input
          type="text"
          value={newThreadTitle}
          onChange={(e) => setNewThreadTitle(e.target.value)}
          placeholder="New thread title (e.g., 'NU thesis review')"
          className="flex-1 rounded border border-slate-300 bg-white px-2 py-1 text-sm"
        />
        <SecondaryButton
          onClick={() => createSession.mutate(newThreadTitle)}
          disabled={createSession.isPending}
        >
          {createSession.isPending ? "Creating…" : "+ Start thread"}
        </SecondaryButton>
      </div>

      <div
        ref={transcriptRef}
        className="max-h-[40vh] min-h-[140px] overflow-y-auto px-4 py-3"
      >
        {activeSessionId === null ? (
          <p className="text-xs text-slate-500">
            No thread selected. Pick one above or start a new thread to begin.
          </p>
        ) : turns.isLoading ? (
          <p className="text-xs text-slate-500">Loading transcript…</p>
        ) : visibleTurns.length === 0 && pendingUser === null ? (
          <p className="text-xs text-slate-500">
            Empty thread. Type a message below — the first turn attaches a
            full snapshot of your portfolio as context.
          </p>
        ) : (
          <ul className="space-y-3">
            {visibleTurns.map((t: ChatTurnOut) => (
              <li key={t.turn_id} className="flex flex-col gap-1">
                <div className="text-[10px] uppercase tracking-wider text-slate-500">
                  {t.role === "user" ? "You" : "CIO advisor"}
                  {t.model_used ? (
                    <span className="ml-1 text-slate-400">
                      · {t.model_used}
                    </span>
                  ) : null}
                </div>
                <div
                  className={[
                    "whitespace-pre-wrap rounded-md px-3 py-2 text-sm leading-relaxed",
                    t.role === "user"
                      ? "bg-slate-100 text-slate-800"
                      : "bg-emerald-50/60 text-slate-800 ring-1 ring-emerald-100",
                  ].join(" ")}
                >
                  {t.content}
                </div>
              </li>
            ))}
            {/* In-flight bubbles. Render the user's message immediately
                so it doesn't feel like clicks vanish; the assistant
                bubble fills as tokens stream in. */}
            {pendingUser !== null && (
              <li
                key="pending-user"
                className="flex flex-col gap-1"
                data-testid="pending-user-bubble"
              >
                <div className="text-[10px] uppercase tracking-wider text-slate-500">
                  You
                </div>
                <div className="whitespace-pre-wrap rounded-md bg-slate-100 px-3 py-2 text-sm leading-relaxed text-slate-800">
                  {pendingUser}
                </div>
              </li>
            )}
            {pendingAssistant !== null && (
              <li
                key="pending-assistant"
                className="flex flex-col gap-1"
                data-testid="pending-assistant-bubble"
              >
                <div className="text-[10px] uppercase tracking-wider text-slate-500">
                  CIO advisor
                  <span className="ml-1 text-slate-400">· streaming…</span>
                </div>
                <div className="whitespace-pre-wrap rounded-md bg-emerald-50/60 px-3 py-2 text-sm leading-relaxed text-slate-800 ring-1 ring-emerald-100">
                  {pendingAssistant.length > 0 ? (
                    <>
                      {pendingAssistant}
                      <span className="ml-0.5 inline-block animate-pulse text-emerald-600">
                        ▍
                      </span>
                    </>
                  ) : (
                    <span className="text-slate-400">Thinking…</span>
                  )}
                </div>
              </li>
            )}
          </ul>
        )}
      </div>

      {sendError && (
        <div className="px-4 py-2">
          <ErrorBanner>{sendError}</ErrorBanner>
        </div>
      )}

      <div className="flex flex-col gap-2 border-t border-slate-100 px-4 py-3 sm:flex-row sm:items-end">
        <textarea
          value={draftMessage}
          onChange={(e) => setDraftMessage(e.target.value)}
          rows={2}
          placeholder={
            activeSessionId === null
              ? "Open or start a thread to ask the CIO."
              : "Type your question… (Cmd/Ctrl + Enter to send; prefix with /refresh to re-snapshot context)"
          }
          disabled={activeSessionId === null || streaming}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              void handleSend();
            }
          }}
          className="flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm leading-relaxed disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
        />
        <PrimaryButton
          onClick={() => void handleSend()}
          disabled={
            activeSessionId === null || streaming || !draftMessage.trim()
          }
        >
          {streaming ? "Streaming…" : "Send"}
        </PrimaryButton>
      </div>
    </Card>
  );
}
