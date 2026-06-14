import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { clsx } from 'clsx'
import { listStrategies, updateStrategy } from '../lib/api'

export default function Strategies() {
  const qc = useQueryClient()

  const { data: strategies = [], isPending } = useQuery({
    queryKey: ['strategies'],
    queryFn: listStrategies,
    refetchInterval: 30_000,
  })

  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      updateStrategy(name, { is_enabled: enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['strategies'] }),
  })

  if (isPending) {
    return <div className="p-6 text-slate-500 text-sm">Loading strategies…</div>
  }

  return (
    <div className="p-6 space-y-4">
      <h2 className="text-lg font-semibold text-slate-100">Strategies</h2>

      {strategies.length === 0 ? (
        <div className="bg-[#1a1d27] rounded-lg p-10 border border-slate-700/50 text-center text-slate-500 text-sm">
          No strategies registered yet.
          <br />
          They are auto-created when TradingView fires the first webhook alert.
        </div>
      ) : (
        <div className="space-y-3">
          {strategies.map(s => (
            <div
              key={s.id}
              className="bg-[#1a1d27] rounded-lg p-4 border border-slate-700/50 flex items-center justify-between gap-4"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className={clsx(
                      'w-2 h-2 rounded-full shrink-0',
                      s.is_enabled ? 'bg-emerald-400' : 'bg-slate-600'
                    )}
                  />
                  <span className="font-mono text-slate-100 truncate">{s.name}</span>
                  <span
                    className={clsx(
                      'text-xs px-1.5 py-0.5 rounded',
                      s.is_enabled
                        ? 'bg-emerald-900/50 text-emerald-400'
                        : 'bg-slate-800 text-slate-500'
                    )}
                  >
                    {s.is_enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>
                {s.description && (
                  <div className="text-xs text-slate-500 mt-1 ml-4">{s.description}</div>
                )}
                {s.last_signal && (
                  <div className="text-xs text-slate-600 mt-0.5 ml-4">
                    Last signal:{' '}
                    {new Date(s.last_signal).toLocaleString('en-US', {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </div>
                )}
              </div>

              {/* Toggle switch */}
              <button
                onClick={() => toggle.mutate({ name: s.name, enabled: !s.is_enabled })}
                disabled={toggle.isPending}
                title={s.is_enabled ? 'Disable strategy' : 'Enable strategy'}
                className={clsx(
                  'relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0',
                  s.is_enabled ? 'bg-indigo-600' : 'bg-slate-700',
                  toggle.isPending && 'opacity-50'
                )}
              >
                <span
                  className={clsx(
                    'inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform',
                    s.is_enabled ? 'translate-x-6' : 'translate-x-1'
                  )}
                />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
