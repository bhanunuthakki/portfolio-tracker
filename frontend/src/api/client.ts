import type {
  BetaResult,
  CashflowAuditOut,
  ChatSessionCreateIn,
  ChatSessionOut,
  ChatTurnIn,
  ChatTurnOut,
  ChatTurnPostResponse,
  CioStreamEvent,
  CoachingResult,
  ConsolidatedHoldingOut,
  DataQualityReportOut,
  ExchangePublicTokenOut,
  HoldingOut,
  HumanCapitalOverlapIn,
  HumanCapitalOverlapOut,
  HumanCapitalOverlapsOut,
  InvestmentTransactionOut,
  ItemOut,
  LinkTokenOut,
  MonthlyBriefJobOut,
  MonthlyBriefOut,
  MonthlyBriefSummary,
  PerformanceSeries,
  PolicyOut,
  PolicyWeightIn,
  PositionAlphaResult,
  PositioningOut,
  SecurityClassificationOut,
  TradeAnalysisResult,
  TradeTimelineResult,
  TransactionOverrideIn,
  TransactionOverrideOut,
  DecisionIn,
  DecisionOut,
  DecisionOutcomeIn,
  TradeTagIn,
  TradeTagOut,
  EarningsRow,
  QueueItem,
  ThesisHealthRow,
} from "@/types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  createLinkToken: (profile: "primary" | "spouse" = "primary"): Promise<LinkTokenOut> =>
    request(`/api/plaid/link-token?profile=${profile}`, { method: "POST" }),

  exchangePublicToken: (input: {
    public_token: string;
    institution_id?: string | null;
    institution_name?: string | null;
  }): Promise<ExchangePublicTokenOut> =>
    request("/api/plaid/exchange-token", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  listItems: (): Promise<ItemOut[]> => request("/api/plaid/items"),

  unlinkItem: (itemId: number): Promise<void> =>
    request(`/api/plaid/items/${itemId}`, { method: "DELETE" }),

  setItemDataActive: (itemId: number, isActive: boolean): Promise<ItemOut> =>
    request(`/api/plaid/items/${itemId}/data-active`, {
      method: "PATCH",
      body: JSON.stringify({ is_data_active: isActive }),
    }),

  latestHoldings: (): Promise<ConsolidatedHoldingOut[]> =>
    request("/api/portfolio/holdings"),

  latestHoldingsByAccount: (): Promise<HoldingOut[]> =>
    request("/api/portfolio/holdings/by-account"),

  positioning: (params?: {
    startDate?: string;
    endDate?: string;
  }): Promise<PositioningOut> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    const qs = search.toString();
    return request(`/api/portfolio/positioning${qs ? `?${qs}` : ""}`);
  },

  setSecurityClassification: (input: {
    security_id: number;
    sector?: string | null;
    region?: string | null;
    notes?: string | null;
  }): Promise<SecurityClassificationOut> =>
    request("/api/overrides/security-classification", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteSecurityClassification: (securityId: number): Promise<void> =>
    request(`/api/overrides/security-classification/${securityId}`, {
      method: "DELETE",
    }),

  cashflowAudit: (): Promise<CashflowAuditOut> =>
    request("/api/portfolio/cashflow-audit"),

  dataQuality: (): Promise<DataQualityReportOut> =>
    request("/api/portfolio/data-quality"),

  beta: (params?: {
    startDate?: string;
    endDate?: string;
    benchmark?: string;
    riskFreeAnnual?: number;
    excludeIndexEtfs?: boolean;
    reserveAmount?: number;
  }): Promise<BetaResult> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.benchmark) search.set("benchmark", params.benchmark);
    if (params?.riskFreeAnnual !== undefined) {
      search.set("risk_free_annual", String(params.riskFreeAnnual));
    }
    if (params?.excludeIndexEtfs) {
      search.set("exclude_index_etfs", "true");
    }
    if (params?.reserveAmount !== undefined && params.reserveAmount > 0) {
      search.set("reserve_amount", String(params.reserveAmount));
    }
    const qs = search.toString();
    return request(`/api/portfolio/beta${qs ? `?${qs}` : ""}`);
  },

  getPolicy: (): Promise<PolicyOut> => request("/api/policy"),

  putPolicy: (weights: PolicyWeightIn[]): Promise<PolicyOut> =>
    request("/api/policy", { method: "PUT", body: JSON.stringify(weights) }),

  setCostBasisOverride: (input: {
    account_id: number;
    security_id: number;
    total_cost_basis: number;
    notes?: string | null;
    acquired_at?: string | null;
  }): Promise<unknown> =>
    request("/api/overrides/cost-basis", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteCostBasisOverride: (
    accountId: number,
    securityId: number,
  ): Promise<void> =>
    request(`/api/overrides/cost-basis/${accountId}/${securityId}`, {
      method: "DELETE",
    }),

  setTickerOverride: (input: {
    security_id: number;
    ticker: string;
    notes?: string | null;
  }): Promise<unknown> =>
    request("/api/overrides/ticker", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteTickerOverride: (securityId: number): Promise<void> =>
    request(`/api/overrides/ticker/${securityId}`, { method: "DELETE" }),

  listTransactionOverrides: (): Promise<TransactionOverrideOut[]> =>
    request("/api/overrides/transactions"),

  setTransactionOverride: (
    input: TransactionOverrideIn,
  ): Promise<TransactionOverrideOut> =>
    request("/api/overrides/transactions", {
      method: "PUT",
      body: JSON.stringify(input),
    }),

  deleteTransactionOverride: (
    plaidInvestmentTransactionId: string,
  ): Promise<void> =>
    request(
      `/api/overrides/transactions/${encodeURIComponent(plaidInvestmentTransactionId)}`,
      { method: "DELETE" },
    ),

  // earnings-summary companion project
  listBriefsForTicker: (
    ticker: string,
  ): Promise<{ ticker: string; briefs: { iso_date: string; path: string }[] }> =>
    request(
      `/api/earnings-summary/briefs?ticker=${encodeURIComponent(ticker)}`,
    ),

  transactions: (params?: {
    startDate?: string;
    endDate?: string;
    limit?: number;
  }): Promise<InvestmentTransactionOut[]> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return request(`/api/portfolio/transactions${qs ? `?${qs}` : ""}`);
  },

  positionAlpha: (params?: {
    startDate?: string;
    endDate?: string;
    excludeBroadIndex?: boolean;
  }): Promise<PositionAlphaResult> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.excludeBroadIndex) search.set("exclude_broad_index", "true");
    const qs = search.toString();
    return request(`/api/portfolio/position-alpha${qs ? `?${qs}` : ""}`);
  },

  performance: (params?: {
    startDate?: string;
    endDate?: string;
    includeBackfill?: boolean;
    reserveAmount?: number;
    excludeIndexEtfs?: boolean;
  }): Promise<PerformanceSeries> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    if (params?.includeBackfill !== undefined) {
      search.set("include_backfill", String(params.includeBackfill));
    }
    if (params?.reserveAmount !== undefined && params.reserveAmount > 0) {
      search.set("reserve_amount", String(params.reserveAmount));
    }
    if (params?.excludeIndexEtfs) {
      search.set("exclude_index_etfs", "true");
    }
    const qs = search.toString();
    return request(`/api/portfolio/performance${qs ? `?${qs}` : ""}`);
  },

  tradeAnalysis: (params?: {
    startDate?: string;
    endDate?: string;
  }): Promise<TradeAnalysisResult> => {
    const search = new URLSearchParams();
    if (params?.startDate) search.set("start_date", params.startDate);
    if (params?.endDate) search.set("end_date", params.endDate);
    const qs = search.toString();
    return request(`/api/portfolio/trade-analysis${qs ? `?${qs}` : ""}`);
  },

  tradeTimeline: (params?: {
    year?: number;
    includeOpen?: boolean;
  }): Promise<TradeTimelineResult> => {
    const search = new URLSearchParams();
    if (params?.year !== undefined) search.set("year", String(params.year));
    if (params?.includeOpen === false) search.set("include_open", "false");
    const qs = search.toString();
    return request(`/api/portfolio/trade-timeline${qs ? `?${qs}` : ""}`);
  },

  // SnapTrade
  snaptradeStatus: (): Promise<{ configured: boolean }> =>
    request("/api/snaptrade/status"),

  snaptradePortalUrl: (
    profile: "primary" | "spouse" = "primary",
  ): Promise<{ url: string }> =>
    request(`/api/snaptrade/connection-portal-url?profile=${profile}`, { method: "POST" }),

  snaptradeSync: (
    profile: "primary" | "spouse" = "primary",
  ): Promise<{
    profile: string;
    items_synced: number;
    accounts_synced: number;
    holdings_written: number;
    transactions_written: number;
  }> => request(`/api/snaptrade/sync?profile=${profile}`, { method: "POST" }),

  // ---- Decision support ------------------------------------------------

  listDecisions: (params?: {
    ticker?: string;
    limit?: number;
  }): Promise<DecisionOut[]> => {
    const search = new URLSearchParams();
    if (params?.ticker) search.set("ticker", params.ticker);
    if (params?.limit) search.set("limit", String(params.limit));
    const qs = search.toString();
    return request(`/api/decisions${qs ? `?${qs}` : ""}`);
  },

  createDecision: (payload: DecisionIn): Promise<DecisionOut> =>
    request("/api/decisions", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  attachOutcome: (
    decisionId: number,
    payload: DecisionOutcomeIn,
  ): Promise<DecisionOut> =>
    request(`/api/decisions/${decisionId}/outcome`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  deleteDecision: (decisionId: number): Promise<void> =>
    request(`/api/decisions/${decisionId}`, { method: "DELETE" }),

  tagVocabulary: (): Promise<string[]> => request("/api/trade-tags/vocabulary"),

  listTradeTags: (ticker?: string): Promise<TradeTagOut[]> => {
    const qs = ticker ? `?ticker=${encodeURIComponent(ticker)}` : "";
    return request(`/api/trade-tags${qs}`);
  },

  createTradeTag: (payload: TradeTagIn): Promise<TradeTagOut> =>
    request("/api/trade-tags", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  deleteTradeTag: (tagId: number): Promise<void> =>
    request(`/api/trade-tags/${tagId}`, { method: "DELETE" }),

  upcomingEarnings: (params?: {
    daysAhead?: number;
    onlyHeld?: boolean;
  }): Promise<EarningsRow[]> => {
    const search = new URLSearchParams();
    if (params?.daysAhead) search.set("days_ahead", String(params.daysAhead));
    if (params?.onlyHeld !== undefined)
      search.set("only_held", String(params.onlyHeld));
    const qs = search.toString();
    return request(`/api/earnings/upcoming${qs ? `?${qs}` : ""}`);
  },

  coachingTips: (): Promise<CoachingResult> => request("/api/coaching/tips"),

  // ---- Cockpit (P3 action queue) --------------------------------------

  cockpitQueue: (): Promise<QueueItem[]> => request("/api/cockpit/queue"),

  cockpitRefresh: (useLlm: boolean): Promise<QueueItem[]> =>
    request(`/api/cockpit/queue/refresh?use_llm=${useLlm}`, { method: "POST" }),

  cockpitAccept: (id: number): Promise<QueueItem> =>
    request(`/api/cockpit/items/${id}/accept`, { method: "POST" }),

  cockpitDismiss: (id: number): Promise<QueueItem> =>
    request(`/api/cockpit/items/${id}/dismiss`, { method: "POST" }),

  cockpitSnooze: (id: number, days = 7): Promise<QueueItem> =>
    request(`/api/cockpit/items/${id}/snooze?days=${days}`, { method: "POST" }),

  cockpitSetExecution: (id: number, status: string): Promise<QueueItem> =>
    request(`/api/cockpit/items/${id}/execution?status=${status}`, {
      method: "POST",
    }),

  thesisHealth: (): Promise<ThesisHealthRow[]> =>
    request("/api/cockpit/thesis-health"),

  // ---- Human-capital overlap buckets ----------------------------------

  listHumanCapitalOverlaps: (): Promise<HumanCapitalOverlapsOut> =>
    request("/api/human-capital/overlaps"),

  upsertHumanCapitalOverlap: (
    body: HumanCapitalOverlapIn,
  ): Promise<HumanCapitalOverlapOut> =>
    request("/api/human-capital/overlaps", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteHumanCapitalOverlap: (ticker: string): Promise<void> =>
    request(`/api/human-capital/overlaps/${encodeURIComponent(ticker)}`, {
      method: "DELETE",
    }),

  // CIO advisor (LLM-powered chat + monthly brief)

  cioListSessions: (): Promise<ChatSessionOut[]> =>
    request("/api/cio-advisor/sessions"),

  cioCreateSession: (payload: ChatSessionCreateIn): Promise<ChatSessionOut> =>
    request("/api/cio-advisor/sessions", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  cioDeleteSession: (sessionId: number): Promise<void> =>
    request(`/api/cio-advisor/sessions/${sessionId}`, { method: "DELETE" }),

  cioListTurns: (sessionId: number): Promise<ChatTurnOut[]> =>
    request(`/api/cio-advisor/sessions/${sessionId}/turns`),

  cioSendTurn: (sessionId: number, payload: ChatTurnIn): Promise<ChatTurnPostResponse> =>
    request(`/api/cio-advisor/sessions/${sessionId}/turns`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /**
   * Stream a chat turn via SSE. POST body carries the user message; the
   * response is `text/event-stream` with one `data: {json}\n\n` event
   * per chunk. The signal lets the caller abort (e.g. on unmount).
   *
   * Yielded shapes (typed loosely so the caller can branch):
   *   { user_turn_id: number }           — first event
   *   { text: string }                   — token chunk (one or more)
   *   { error: string }                  — non-fatal; still followed by `done`
   *   { done: true, turn_id: number }    — terminal event
   */
  cioStreamTurn: async function* cioStreamTurn(
    sessionId: number,
    payload: ChatTurnIn,
    signal?: AbortSignal,
  ): AsyncGenerator<CioStreamEvent, void, void> {
    const resp = await fetch(
      `/api/cio-advisor/sessions/${sessionId}/turns/stream`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal,
      },
    );
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
    }
    if (!resp.body) {
      throw new Error("Streaming response has no body.");
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    // SSE events are delimited by a blank line. Buffer partial events
    // across reads — a single chunk might split mid-event.
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const eventBlock = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          // An event block can have multiple lines (`data:`, `event:`, etc.).
          // We only emit `data:` lines; per SSE the runtime concatenates
          // multiple data lines, but our server only emits one per event.
          for (const line of eventBlock.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trimStart();
            if (!payload) continue;
            try {
              yield JSON.parse(payload) as CioStreamEvent;
            } catch {
              // Malformed event — skip rather than break the stream.
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  },

  cioListBriefs: (): Promise<MonthlyBriefSummary[]> =>
    request("/api/cio-advisor/briefs"),

  cioGetBrief: (briefId: number): Promise<MonthlyBriefOut> =>
    request(`/api/cio-advisor/briefs/${briefId}`),

  cioGenerateBrief: (periodYyyymm?: string): Promise<MonthlyBriefJobOut> => {
    const qs = periodYyyymm ? `?period_yyyymm=${encodeURIComponent(periodYyyymm)}` : "";
    return request(`/api/cio-advisor/briefs/generate${qs}`, { method: "POST" });
  },

  cioGetBriefJob: (jobId: number): Promise<MonthlyBriefJobOut> =>
    request(`/api/cio-advisor/briefs/jobs/${jobId}`),

  // Statuses is comma-joined server-side: e.g. ["pending", "running"]
  // → ?status=pending,running. Used on mount to detect in-flight jobs
  // and resume polling after a page reload.
  cioListBriefJobs: (statuses?: string[]): Promise<MonthlyBriefJobOut[]> => {
    const qs =
      statuses && statuses.length > 0
        ? `?status=${encodeURIComponent(statuses.join(","))}`
        : "";
    return request(`/api/cio-advisor/briefs/jobs${qs}`);
  },
};
