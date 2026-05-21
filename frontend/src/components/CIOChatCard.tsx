import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

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
 * The Claude call is sync subprocess on the backend (~5–15s for Sonnet),
 * so the send button shows a spinner during the round-trip.
 */
export function CIOChatCard(): JSX.Element {
  const queryClient = useQueryClient();
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [newThreadTitle, setNewThreadTitle] = useState("");
  const [draftMessage, setDraftMessage] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);

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

  // Auto-scroll the transcript to the bottom when a new turn arrives.
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [visibleTurns.length]);

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

  const sendTurn = useMutation({
    mutationFn: ({ sessionId, content }: { sessionId: number; content: string }) =>
      api.cioSendTurn(sessionId, { content }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cio-turns", activeSessionId] });
      queryClient.invalidateQueries({ queryKey: ["cio-sessions"] });
      setDraftMessage("");
      setSendError(null);
    },
    onError: (err) => {
      setSendError(err instanceof Error ? err.message : "Send failed.");
    },
  });

  const handleSend = (): void => {
    if (activeSessionId === null) {
      setSendError("Open a thread first.");
      return;
    }
    if (!draftMessage.trim()) return;
    sendTurn.mutate({ sessionId: activeSessionId, content: draftMessage });
  };

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
        ) : visibleTurns.length === 0 ? (
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
          disabled={activeSessionId === null || sendTurn.isPending}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              handleSend();
            }
          }}
          className="flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm leading-relaxed disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
        />
        <PrimaryButton
          onClick={handleSend}
          disabled={
            activeSessionId === null ||
            sendTurn.isPending ||
            !draftMessage.trim()
          }
        >
          {sendTurn.isPending ? "Thinking…" : "Send"}
        </PrimaryButton>
      </div>
    </Card>
  );
}
