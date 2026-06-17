import { useQueryClient, useQuery } from '@tanstack/react-query'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { Activity, TrendingUp, TrendingDown } from 'lucide-react'
import { clsx } from 'clsx'
import { useAccount } from '../lib/context'
import { getPortfolio, getEquityCurve, listSignals } from '../lib/api'
import { useWebSocket } from '../hooks/useWebSocket'
import { fmtMoney, fmtTime, fmtDate } from '../lib/format'

function regimeBadge(regime?: string) {
  if (!regime) return <span className="text-slate-500 text-sm">—</span>
  const map: Record<string, string> = {
    LOW_VOL: 'bg-emerald-900/50 text-emerald-400 border-emerald-800',
    MID_VOL: 'bg-amber-900/50 text-amber-400 border-amber-800',
    HIGH_VOL: 'bg-red-900/50 text-red-400 border-red-800',
  }
  const label: Record<string, string> = {
    LOW_VOL: 'Low Vol',
    MID_VOL: 'Mid Vol',
    HIGH_VOL: 'High Vol',
  }
  const cls = map[regime] ?? 'bg-slate-800 text-slate-400 border-slate-700'
  return (
    <span className={`px-2 py-1 rounded border text-xs font-medium ${cls}`}>
      {label[regime] ?? regime}
    </span>
  )
}

function cbBadge(cb: string) {
  const map: Record<string, string> = {
    NONE: 'bg-slate-800 text-slate-400 border-slate-700',
    DAILY_REDUCE: 'bg-amber-900/50 text-amber-400 border-amber-800',
    DAILY_HALT: 'bg-orange-900/50 text-orange-400 border-orange-800',
    WEEKLY_REDUCE: 'bg-orange-900/50 text-orange-400 border-orange-800',
    WEEKLY_HALT: 'bg-red-900/50 text-red-400 border-red-800',
    PEAK_HALT: 'bg-red-950/80 text-red-400 border-red-700',
  }
  const label: Record<string, string> = {
    NONE: 'Normal',
    DAILY_REDUCE: 'Daily Reduce',
    DAILY_HALT: 'Daily Halt',
    WEEKLY_REDUCE: 'Weekly Reduce',
    WEEKLY_HALT: 'Weekly Halt',
    PEAK_HALT: '⚠ Peak Halt',
  }
  const cls = map[cb] ?? 'bg-slate-800 text-slate-400 border-slate-700'
  return (
    <span className={`px-2 py-1 rounded border text-xs font-medium ${cls}`}>
      {label[cb] ?? cb}
    </span>
  )
}


export default function Dashboard() {
  const { accountId } = useAccount()
  const qc = useQueryClient()

  const { data: portfolio } = useQuery({
    queryKey: ['portfolio', accountId],
    queryFn: () => getPortfolio(accountId),
    enabled: accountId > 0,
    refetchInterval: 30_000,
  })

  const { data: equityCurve = [] } = useQuery({
    queryKey: ['equity', accountId],
    queryFn: () => getEquityCurve(accountId, 200),
    enabled: accountId > 0,
    refetchInterval: 60_000,
  })

  const { data: signals = [] } = useQuery({
    queryKey: ['signals', accountId],
    queryFn: () => listSignals(accountId, { limit: 15 }),
    enabled: accountId > 0,
    refetchInterval: 15_000,
  })

  useWebSocket(msg => {
    if (msg.type === 'signal' || msg.type === 'equity') {
      void qc.invalidateQueries({ queryKey: ['portfolio', accountId] })
      void qc.invalidateQueries({ queryKey: ['signals', accountId] })
      void qc.invalidateQueries({ queryKey: ['equity', accountId] })
    }
  })

  const pnl = portfolio?.strategy_pnl ?? 0
  const pnlUp = pnl >= 0

  return (
    <div className="p-6 space-y-6">
      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-1">Strategy Equity</div>
          <div className="text-2xl font-semibold text-slate-100">
            {fmtMoney(portfolio?.strategy_equity)}
          </div>
          {portfolio?.last_updated && (
            <div className="text-xs text-slate-600 mt-1">
              as of {fmtTime(portfolio.last_updated)}
            </div>
          )}
        </div>

        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-1">Session P&L</div>
          <div
            className={clsx(
              'text-2xl font-semibold flex items-center gap-1',
              pnlUp ? 'text-emerald-400' : 'text-red-400'
            )}
          >
            {pnlUp ? (
              <TrendingUp className="w-5 h-5" />
            ) : (
              <TrendingDown className="w-5 h-5" />
            )}
            {fmtMoney(portfolio?.strategy_pnl)}
          </div>
        </div>

        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-2">Regime</div>
          {regimeBadge(portfolio?.regime)}
        </div>

        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-2">Circuit Breaker</div>
          {cbBadge(portfolio?.circuit_breaker ?? 'NONE')}
        </div>
      </div>

      {/* Equity curve + Signal feed */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-sm font-medium text-slate-300 mb-3">Equity Curve</div>
          {equityCurve.length === 0 ? (
            <div className="h-48 flex items-center justify-center text-slate-500 text-sm">
              No equity snapshots yet — send a TradingView alert to start tracking
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={equityCurve} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
                <defs>
                  <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis
                  dataKey="recorded_at"
                  tickFormatter={fmtDate}
                  tick={{ fill: '#64748b', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                  tick={{ fill: '#64748b', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  width={48}
                />
                <Tooltip
                  contentStyle={{
                    background: '#1e2130',
                    border: '1px solid #334155',
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  labelStyle={{ color: '#94a3b8' }}
                  itemStyle={{ color: '#e2e8f0' }}
                  labelFormatter={fmtDate}
                  formatter={(v: number) => [`$${v.toLocaleString()}`, 'Equity'] as [string, string]}
                />
                <Area
                  type="monotone"
                  dataKey="equity"
                  stroke="#6366f1"
                  strokeWidth={2}
                  fill="url(#eqGrad)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Signal feed */}
        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50 flex flex-col">
          <div className="flex items-center gap-2 mb-3">
            <Activity className="w-4 h-4 text-indigo-400" />
            <span className="text-sm font-medium text-slate-300">Live Signals</span>
          </div>
          <div className="flex-1 overflow-y-auto space-y-2 max-h-52">
            {signals.length === 0 ? (
              <div className="text-slate-500 text-sm text-center py-6">No signals yet</div>
            ) : (
              signals.map(sig => (
                <div
                  key={sig.id}
                  className="flex items-center justify-between text-xs border-b border-slate-700/30 pb-2"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={clsx(
                        'font-mono font-bold w-8',
                        sig.action === 'buy'
                          ? 'text-emerald-400'
                          : sig.action === 'sell'
                          ? 'text-red-400'
                          : 'text-amber-400'
                      )}
                    >
                      {sig.action.toUpperCase()}
                    </span>
                    <span className="text-slate-200 font-mono">{sig.symbol}</span>
                    {sig.regime && (
                      <span className="text-slate-600">{sig.regime.replace('_', ' ')}</span>
                    )}
                  </div>
                  <span className="text-slate-600">{fmtTime(sig.received_at)}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Open positions */}
      <div className="bg-[#1a1d27] rounded-lg border border-slate-700/50">
        <div className="px-4 py-3 border-b border-slate-700/50 flex items-center gap-2">
          <span className="text-sm font-medium text-slate-300">Open Positions</span>
          <span className="text-xs text-slate-500">
            ({portfolio?.open_positions?.length ?? 0})
          </span>
        </div>
        {!portfolio?.open_positions?.length ? (
          <div className="p-8 text-center text-slate-500 text-sm">No open positions</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 uppercase tracking-wide">
                  <th className="px-4 py-2 text-left">Symbol</th>
                  <th className="px-4 py-2 text-left">Dir</th>
                  <th className="px-4 py-2 text-right">Qty</th>
                  <th className="px-4 py-2 text-right">Entry</th>
                  <th className="px-4 py-2 text-right">Unrealised P&L</th>
                  <th className="px-4 py-2 text-left">Regime</th>
                  <th className="px-4 py-2 text-left">Opened</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.open_positions.map(pos => (
                  <tr
                    key={pos.trade_id}
                    className="border-t border-slate-700/30 hover:bg-slate-800/30"
                  >
                    <td className="px-4 py-2 font-mono text-slate-100">{pos.symbol}</td>
                    <td className="px-4 py-2">
                      <span
                        className={clsx(
                          'text-xs font-medium',
                          pos.direction === 'long' ? 'text-emerald-400' : 'text-red-400'
                        )}
                      >
                        {pos.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right text-slate-300">{pos.contracts}</td>
                    <td className="px-4 py-2 text-right font-mono text-slate-300">
                      ${pos.entry_price.toFixed(2)}
                    </td>
                    <td
                      className={clsx(
                        'px-4 py-2 text-right font-mono',
                        (pos.unrealised_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                      )}
                    >
                      {fmtMoney(pos.unrealised_pnl)}
                    </td>
                    <td className="px-4 py-2 text-slate-500 text-xs">
                      {pos.regime_at_entry ?? '—'}
                    </td>
                    <td className="px-4 py-2 text-slate-500 text-xs">
                      {fmtTime(pos.opened_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
