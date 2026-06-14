import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

export interface Account {
  id: number
  name: string
  broker: string
  environment: string
  notes?: string
  is_active: boolean
  created_at: string
}

export interface OpenPosition {
  trade_id: number
  symbol: string
  direction: string
  contracts: number
  entry_price: number
  strategy_name?: string
  regime_at_entry?: string
  opened_at: string
  point_value: number
  unrealised_pnl?: number
}

export interface Portfolio {
  account_id: number
  account_name: string
  strategy_equity?: number
  strategy_pnl?: number
  regime?: string
  open_positions: OpenPosition[]
  circuit_breaker: string
  last_updated?: string
}

export interface Trade {
  id: number
  account_id: number
  symbol: string
  direction: string
  contracts: number
  entry_price: number
  exit_price?: number
  strategy_name?: string
  regime_at_entry?: string
  point_value: number
  pnl?: number
  pnl_pct?: number
  opened_at: string
  closed_at?: string
  duration_mins?: number
  is_open: boolean
  is_winner?: boolean
}

export interface TradeSummary {
  total_trades: number
  winners: number
  losers: number
  win_rate: number
  total_pnl: number
  avg_winner: number
  avg_loser: number
  max_loss: number
  max_win: number
}

export interface EquityPoint {
  recorded_at: string
  equity: number
  pnl?: number
  regime?: string
}

export interface Signal {
  id: number
  account_id: number
  symbol: string
  action: string
  contracts?: number
  price?: number
  stop_price?: number
  take_profit?: number
  strategy_name?: string
  regime?: string
  strategy_equity?: number
  strategy_pnl?: number
  position_size?: number
  approved: boolean
  rejection_reason?: string
  received_at: string
}

export interface Strategy {
  id: number
  name: string
  description?: string
  is_enabled: boolean
  created_at: string
  last_signal?: string
}

export const listAccounts = () =>
  api.get<Account[]>('/accounts').then(r => r.data)

export const createAccount = (data: {
  name: string
  broker?: string
  environment?: string
  notes?: string
}) => api.post<Account>('/accounts', data).then(r => r.data)

export const updateAccount = (
  id: number,
  data: Partial<{ name: string; environment: string; notes: string; is_active: boolean }>
) => api.patch<Account>(`/accounts/${id}`, data).then(r => r.data)

export const deleteAccount = (id: number) => api.delete(`/accounts/${id}`)

export const getPortfolio = (accountId: number) =>
  api.get<Portfolio>(`/portfolio/${accountId}`).then(r => r.data)

export const listTrades = (
  accountId: number,
  params?: {
    symbol?: string
    open_only?: boolean
    closed_only?: boolean
    limit?: number
    offset?: number
  }
) => api.get<Trade[]>(`/trades/${accountId}`, { params }).then(r => r.data)

export const getTradeSummary = (accountId: number) =>
  api.get<TradeSummary>(`/trades/${accountId}/summary`).then(r => r.data)

export const getEquityCurve = (accountId: number, limit?: number) =>
  api
    .get<EquityPoint[]>(`/trades/${accountId}/equity`, { params: { limit } })
    .then(r => r.data)

export const listSignals = (
  accountId: number,
  params?: { symbol?: string; limit?: number; offset?: number }
) => api.get<Signal[]>(`/signals/${accountId}`, { params }).then(r => r.data)

export const listStrategies = () =>
  api.get<Strategy[]>('/strategies').then(r => r.data)

export const updateStrategy = (
  name: string,
  data: { is_enabled?: boolean; description?: string }
) => api.patch<Strategy>(`/strategies/${name}`, data).then(r => r.data)
