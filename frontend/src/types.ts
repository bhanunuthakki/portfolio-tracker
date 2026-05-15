export interface AccountOut {
  account_id: number;
  name: string;
  official_name: string | null;
  type: string;
  subtype: string | null;
  mask: string | null;
  currency: string;
}

export interface ItemOut {
  item_id: number;
  source: string;
  institution_name: string | null;
  plaid_institution_id: string | null;
  linked_at: string;
  last_refreshed_at: string | null;
  is_data_active: boolean;
  accounts: AccountOut[];
}

export interface HoldingOut {
  snapshot_date: string;
  account_id: number;
  account_name: string;
  security_id: number;
  ticker: string | null;
  name: string | null;
  quantity: string;
  institution_price: string | null;
  institution_value: string | null;
  cost_basis: string | null;
  currency: string;
}

export interface HoldingByAccountOut {
  account_id: number;
  account_name: string;
  quantity: string;
  institution_value: string | null;
  cost_basis: string | null;
}

export interface ConsolidatedHoldingOut {
  snapshot_date: string;
  security_id: number;
  ticker: string | null;
  name: string | null;
  total_quantity: string;
  total_value: string | null;
  total_cost_basis: string | null;
  weighted_avg_cost_per_share: string | null;
  unrealized_pnl: string | null;
  accounts: HoldingByAccountOut[];
  currency: string;
}

export interface CashflowGroupOut {
  type: string;
  subtype: string | null;
  count: number;
  sum_amount: string;
  classified_as_external_cashflow: boolean;
}

export interface CashflowAuditOut {
  start_date: string;
  end_date: string;
  groups: CashflowGroupOut[];
  net_external_cashflow_in: string;
  notes: string[];
}

export interface DataQualityFindingOut {
  category: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  recommended_action: string | null;
  context: Record<string, string>;
}

export interface DataQualityReportOut {
  generated_at: string;
  findings: DataQualityFindingOut[];
  summary_counts: Record<string, number>;
}

export interface BetaResult {
  benchmark: string;
  start_date: string;
  end_date: string;
  sample_size: number;
  risk_free_annual: number;
  beta: number | null;
  alpha_annualized_pct: number | null;
  r_squared: number | null;
  correlation: number | null;
  sharpe: number | null;
  sortino: number | null;
  information_ratio: number | null;
  portfolio_volatility_annualized: number | null;
  benchmark_volatility_annualized: number | null;
  tracking_error_annualized: number | null;
  notes: string[];
}

export interface PolicyWeightOut {
  ticker: string;
  weight_pct: string;
  notes: string | null;
  updated_at: string;
}

export interface PolicyOut {
  weights: PolicyWeightOut[];
  total_pct: string;
  is_balanced: boolean;
}

export interface PolicyWeightIn {
  ticker: string;
  weight_pct: number;
  notes?: string | null;
}

export type TxClassification = "external_in" | "external_out" | "internal";

export interface InvestmentTransactionOut {
  plaid_investment_transaction_id: string;
  account_id: number;
  account_name: string;
  security_id: number | null;
  ticker: string | null;
  date: string;
  name: string | null;
  quantity: string;
  amount: string;
  price: string | null;
  fees: string | null;
  type: string;
  subtype: string | null;
  currency: string;
  override_classification: TxClassification | null;
  effective_classification: TxClassification | null;
}

export interface TransactionOverrideIn {
  plaid_investment_transaction_id: string;
  classification: TxClassification;
  notes?: string | null;
}

export interface TransactionOverrideOut {
  plaid_investment_transaction_id: string;
  classification: TxClassification;
  notes: string | null;
  updated_at: string;
  tx_date: string | null;
  tx_type: string | null;
  tx_subtype: string | null;
  tx_amount: string | null;
  account_name: string | null;
  ticker: string | null;
}

export interface PerformancePoint {
  date: string;
  portfolio_value: string;
  portfolio_return_pct: string;
  spy_return_pct: string | null;
  qqq_return_pct: string | null;
  policy_return_pct: string | null;
  spy_equivalent_value: string | null;
  qqq_equivalent_value: string | null;
  policy_equivalent_value: string | null;
}

export interface PerformanceSeries {
  start_date: string;
  end_date: string;
  base_value: string;
  points: PerformancePoint[];
  earliest_observed_date: string | null;
  net_external_cashflow_in: string;
  backfill_start_unreliable: boolean;
}

export interface LinkTokenOut {
  link_token: string;
}

export interface ExchangePublicTokenOut {
  item_id: number;
  accounts_linked: number;
}

export interface TickerTrade {
  ticker: string;
  name: string | null;
  first_buy: string | null;
  last_action: string | null;
  bought_total: string;
  sold_total: string;
  today_qty: string;
  today_value: string;
  pnl_dollars: string;
  pnl_pct: number | null;
  trade_count: number;
  is_open: boolean;
  cost_basis_unreliable: boolean;
}

export interface TradingActivity {
  start_date: string;
  end_date: string;
  total_trades: number;
  total_notional: string;
  average_position_value: string | null;
  annualized_turnover_pct: number | null;
  trade_count_by_month: Record<string, number>;
}

export interface TradeAnalysisResult {
  start_date: string;
  end_date: string;
  activity: TradingActivity;
  tickers: TickerTrade[];
  notes: string[];
}

export interface TimelineRow {
  row_kind: "closed" | "open";
  ticker: string | null;
  name: string | null;
  description: string;
  acquired_date: string | null;
  disposed_date: string;
  acquired_approx: boolean;
  holding_days: number;
  quantity: string | null;
  proceeds: string;
  cost_basis: string;
  realized_gain: string;
  return_pct: number | null;
  spy_start_price: string | null;
  spy_end_price: string | null;
  spy_return_pct: number | null;
  spy_counterfactual_dollars: string | null;
  alpha_dollars: string | null;
  source: "1099" | "broker";
  broker: string | null;
  tax_year: number | null;
  term: string | null;
}

export interface YearSummary {
  year: number;
  closed_count: number;
  realized_total: string;
  spy_counterfactual_total: string;
  alpha_total: string;
}

export interface TradeTimelineResult {
  rows: TimelineRow[];
  by_year: YearSummary[];
  notes: string[];
}

export interface DecisionOut {
  decision_id: number;
  decision_date: string;
  ticker: string;
  action: string;
  thesis: string;
  expected_outcome: string | null;
  invalidation_triggers: string | null;
  position_size_pct: string | null;
  time_horizon_months: number | null;
  confidence: string | null;
  notes: string | null;
  outcome_date: string | null;
  outcome_status: string | null;
  outcome_notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface DecisionIn {
  decision_date: string;
  ticker: string;
  action: string;
  thesis: string;
  expected_outcome?: string | null;
  invalidation_triggers?: string | null;
  position_size_pct?: number | null;
  time_horizon_months?: number | null;
  confidence?: string | null;
  notes?: string | null;
}

export interface DecisionOutcomeIn {
  outcome_date: string;
  outcome_status: string;
  outcome_notes?: string | null;
}

export interface TradeTagOut {
  tag_id: number;
  ticker: string;
  period_start: string;
  period_end: string | null;
  tag: string;
  notes: string | null;
  created_at: string;
}

export interface TradeTagIn {
  ticker: string;
  period_start: string;
  period_end?: string | null;
  tag: string;
  notes?: string | null;
}

export interface EarningsRow {
  ticker: string;
  name: string | null;
  earnings_date: string;
  earnings_time: string | null;
  eps_estimate: string | null;
  eps_actual: string | null;
  revenue_estimate: string | null;
  revenue_actual: string | null;
  days_until: number;
  held_qty: string | null;
  held_value: string | null;
}
