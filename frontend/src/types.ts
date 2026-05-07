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
  institution_name: string | null;
  plaid_institution_id: string | null;
  linked_at: string;
  last_refreshed_at: string | null;
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
