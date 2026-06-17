import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { clsx } from 'clsx'
import { useAccount } from '../lib/context'
import { listTrades, getTradeSummary } from '../lib/api'
import { fmtMoney, fmtPct, fmtDate } from '../lib/format'

const PAGE_SIZE = 20

type Filter = 'all' | 'winners' | 'losers'

export default function Trades() {
  const { accountId } = useAccount()
  const [symbol, setSymbol] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [page, setPage] = useState(0)

  const { data: summary } = useQuery({
    queryKey: ['trade-summary', accountId],
    queryFn: () => getTradeSummary(accountId),
    enabled: accountId > 0,
    refetchInterval: 60_000,
  })

  const { data: trades = [], isPending } = useQuery({
    queryKey: ['trades', accountId, symbol, page],
    queryFn: () =>
      listTrades(accountId, {
        symbol: symbol || undefined,
        closed_only: true,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    enabled: accountId > 0,
  })

  const filtered =
    filter === 'winners'
      ? trades.filter(t => t.is_winner === true)
      : filter === 'losers'
      ? trades.filter(t => t.is_winner === false)
      : trades

  const statCards = [
    {
      label: 'Win Rate',
      value: summary ? `${(summary.win_rate * 100).toFixed(1)}%` : '—',
      color: '',
    },
    {
      label: 'Total P&L',
      value: fmtMoney(summary?.total_pnl),
      color: (summary?.total_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400',
    },
    {
      label: 'Avg Winner',
      value: fmtMoney(summary?.avg_winner),
      color: 'text-emerald-400',
    },
    {
      label: 'Avg Loser',
      value: fmtMoney(summary?.avg_loser),
      color: 'text-red-400',
    },
    {
      label: 'Max Win',
      value: fmtMoney(summary?.max_win),
      color: 'text-emerald-400',
    },
    {
      label: 'Max Loss',
      value: fmtMoney(summary?.max_loss),
      color: 'text-red-400',
    },
  ]

  return (
    <div className="p-6 space-y-6">
      {/* Summary stats */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {statCards.map(({ label, value, color }) => (
          <div
            key={label}
            className="bg-[#1a1d27] rounded-lg p-3 border border-slate-700/50"
          >
            <div className="text-xs text-slate-500 mb-1">{label}</div>
            <div className={clsx('text-lg font-semibold', color || 'text-slate-100')}>
              {value}
            </div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="text"
          placeholder="Filter by symbol…"
          value={symbol}
          onChange={e => {
            setSymbol(e.target.value)
            setPage(0)
          }}
          className="bg-[#1a1d27] border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
        />
        <div className="flex rounded overflow-hidden border border-slate-700">
          {(['all', 'winners', 'losers'] as Filter[]).map(f => (
            <button
              key={f}
              onClick={() => {
                setFilter(f)
                setPage(0)
              }}
              className={clsx(
                'px-3 py-1.5 text-xs capitalize',
                filter === f
                  ? 'bg-indigo-600 text-white'
                  : 'bg-[#1a1d27] text-slate-400 hover:text-slate-200'
              )}
            >
              {f}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500">
          {summary?.total_trades ?? 0} total closed trades
        </span>
      </div>

      {/* Trade table */}
      <div className="bg-[#1a1d27] rounded-lg border border-slate-700/50">
        {isPending ? (
          <div className="p-10 text-center text-slate-500 text-sm">Loading trades…</div>
        ) : filtered.length === 0 ? (
          <div className="p-10 text-center text-slate-500 text-sm">No trades found</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 uppercase tracking-wide border-b border-slate-700/50">
                  <th className="px-4 py-3 text-left">#</th>
                  <th className="px-4 py-3 text-left">Symbol</th>
                  <th className="px-4 py-3 text-left">Dir</th>
                  <th className="px-4 py-3 text-right">Qty</th>
                  <th className="px-4 py-3 text-right">Entry</th>
                  <th className="px-4 py-3 text-right">Exit</th>
                  <th className="px-4 py-3 text-right">P&L</th>
                  <th className="px-4 py-3 text-right">P&L %</th>
                  <th className="px-4 py-3 text-left">Regime</th>
                  <th className="px-4 py-3 text-left">Date</th>
                  <th className="px-4 py-3 text-right">Dur.</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map(t => (
                  <tr
                    key={t.id}
                    className="border-t border-slate-700/30 hover:bg-slate-800/30"
                  >
                    <td className="px-4 py-2 text-slate-600">{t.id}</td>
                    <td className="px-4 py-2 font-mono text-slate-100">{t.symbol}</td>
                    <td className="px-4 py-2">
                      <span
                        className={clsx(
                          'text-xs font-medium',
                          t.direction === 'long' ? 'text-emerald-400' : 'text-red-400'
                        )}
                      >
                        {t.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right text-slate-300">{t.contracts}</td>
                    <td className="px-4 py-2 text-right font-mono text-slate-300">
                      {t.entry_price.toFixed(2)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-slate-300">
                      {t.exit_price?.toFixed(2) ?? '—'}
                    </td>
                    <td
                      className={clsx(
                        'px-4 py-2 text-right font-mono',
                        (t.pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                      )}
                    >
                      {fmtMoney(t.pnl)}
                    </td>
                    <td
                      className={clsx(
                        'px-4 py-2 text-right text-xs',
                        (t.pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                      )}
                    >
                      {fmtPct(t.pnl_pct)}
                    </td>
                    <td className="px-4 py-2 text-slate-500 text-xs">
                      {t.regime_at_entry ?? '—'}
                    </td>
                    <td className="px-4 py-2 text-slate-500 text-xs">
                      {fmtDate(t.opened_at)}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-500 text-xs">
                      {t.duration_mins != null ? `${t.duration_mins}m` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="px-4 py-3 border-t border-slate-700/50 flex items-center justify-between">
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 text-sm rounded border border-slate-700 text-slate-400 disabled:opacity-30 hover:border-slate-500 hover:text-slate-200"
          >
            ← Prev
          </button>
          <span className="text-xs text-slate-500">Page {page + 1}</span>
          <button
            onClick={() => setPage(p => p + 1)}
            disabled={trades.length < PAGE_SIZE}
            className="px-3 py-1 text-sm rounded border border-slate-700 text-slate-400 disabled:opacity-30 hover:border-slate-500 hover:text-slate-200"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  )
}
