import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Copy, Check, Plus, Trash2 } from 'lucide-react'
import { clsx } from 'clsx'
import { listAccounts, createAccount, deleteAccount } from '../lib/api'

const WEBHOOK_URL = `${window.location.protocol}//${window.location.hostname}:8000/webhook/alert`

const RISK_THRESHOLDS = [
  { label: 'Daily Reduce', pct: '-2%', color: 'text-amber-400' },
  { label: 'Daily Halt', pct: '-3%', color: 'text-orange-400' },
  { label: 'Weekly Reduce', pct: '-5%', color: 'text-orange-400' },
  { label: 'Weekly Halt', pct: '-7%', color: 'text-red-400' },
  { label: 'Peak Halt', pct: '-10%', color: 'text-red-400' },
]

export default function Settings() {
  const qc = useQueryClient()
  const [copied, setCopied] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newEnv, setNewEnv] = useState<'demo' | 'live'>('demo')

  const { data: accounts = [] } = useQuery({
    queryKey: ['accounts'],
    queryFn: listAccounts,
  })

  const addMutation = useMutation({
    mutationFn: () => createAccount({ name: newName.trim(), environment: newEnv }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      setNewName('')
      setShowAdd(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteAccount(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['accounts'] }),
  })

  const copyWebhook = () => {
    void navigator.clipboard.writeText(WEBHOOK_URL).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div className="p-6 space-y-10 max-w-2xl">
      {/* Accounts */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-100">Accounts</h2>
          <button
            onClick={() => setShowAdd(v => !v)}
            className="flex items-center gap-1 text-sm text-indigo-400 hover:text-indigo-300"
          >
            <Plus className="w-4 h-4" />
            Add Account
          </button>
        </div>

        {showAdd && (
          <div className="bg-[#1a1d27] rounded-lg p-4 border border-indigo-700/50 mb-3 space-y-3">
            <input
              type="text"
              placeholder="Account name"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && newName.trim()) addMutation.mutate()
              }}
              className="w-full bg-[#0f1117] border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
              autoFocus
            />
            <div className="flex items-center gap-2">
              <select
                value={newEnv}
                onChange={e => setNewEnv(e.target.value as 'demo' | 'live')}
                className="bg-[#0f1117] border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-200 focus:outline-none"
              >
                <option value="demo">Demo</option>
                <option value="live">Live</option>
              </select>
              <button
                onClick={() => addMutation.mutate()}
                disabled={!newName.trim() || addMutation.isPending}
                className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm rounded transition-colors"
              >
                {addMutation.isPending ? 'Creating…' : 'Create'}
              </button>
              <button
                onClick={() => {
                  setShowAdd(false)
                  setNewName('')
                }}
                className="px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
            </div>
            {addMutation.isError && (
              <div className="text-xs text-red-400">
                Failed to create account. Name may already exist.
              </div>
            )}
          </div>
        )}

        <div className="space-y-2">
          {accounts.length === 0 && !showAdd && (
            <div className="text-slate-500 text-sm text-center py-4">No accounts yet</div>
          )}
          {accounts.map(a => (
            <div
              key={a.id}
              className="bg-[#1a1d27] rounded-lg px-4 py-3 border border-slate-700/50 flex items-center justify-between"
            >
              <div>
                <div className="text-sm text-slate-100">{a.name}</div>
                <div className="text-xs text-slate-500 mt-0.5">
                  {a.broker} ·{' '}
                  <span
                    className={clsx(
                      a.environment === 'live' ? 'text-amber-400' : 'text-slate-500'
                    )}
                  >
                    {a.environment}
                  </span>
                </div>
              </div>
              <button
                onClick={() => {
                  if (window.confirm(`Delete account "${a.name}"? This cannot be undone.`)) {
                    deleteMutation.mutate(a.id)
                  }
                }}
                disabled={deleteMutation.isPending}
                className="text-slate-600 hover:text-red-400 p-1 transition-colors"
                title="Delete account"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
      </section>

      {/* Webhook URL */}
      <section>
        <h2 className="text-lg font-semibold text-slate-100 mb-4">Webhook URL</h2>
        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-2">
            Paste this URL in the TradingView alert → Webhook URL field:
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 bg-[#0f1117] rounded px-3 py-2 text-sm font-mono text-slate-300 overflow-x-auto whitespace-nowrap">
              {WEBHOOK_URL}
            </code>
            <button
              onClick={copyWebhook}
              className={clsx(
                'flex items-center gap-1 px-3 py-2 rounded text-xs shrink-0 transition-colors',
                copied
                  ? 'bg-emerald-700 text-emerald-100'
                  : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
              )}
            >
              {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
              {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
        </div>
      </section>

      {/* Risk thresholds */}
      <section>
        <h2 className="text-lg font-semibold text-slate-100 mb-4">Risk Thresholds</h2>
        <div className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50">
          <div className="text-xs text-slate-500 mb-3">
            Circuit-breaker levels (session P&L % from equity):
          </div>
          <div className="space-y-2.5">
            {RISK_THRESHOLDS.map(({ label, pct, color }) => (
              <div key={label} className="flex justify-between items-center text-sm">
                <span className="text-slate-400">{label}</span>
                <span className={clsx('font-mono font-medium', color)}>{pct}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 pt-3 border-t border-slate-700/50 text-xs text-slate-600">
            Thresholds are computed server-side in api/routes/portfolio.py
          </div>
        </div>
      </section>
    </div>
  )
}
