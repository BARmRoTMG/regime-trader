import { useState, useEffect } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { BarChart2, Clock, Layers, Settings, Activity } from 'lucide-react'
import { clsx } from 'clsx'
import { AccountContext } from './lib/context'
import { listAccounts } from './lib/api'
import Dashboard from './pages/Dashboard'
import Trades from './pages/Trades'
import Strategies from './pages/Strategies'
import SettingsPage from './pages/Settings'

const NAV = [
  { to: '/', icon: BarChart2, label: 'Dashboard' },
  { to: '/trades', icon: Clock, label: 'Past Trades' },
  { to: '/strategies', icon: Layers, label: 'Strategies' },
  { to: '/settings', icon: Settings, label: 'Settings' },
] as const

export default function App() {
  const [accountId, setAccountId] = useState(0)

  const { data: accounts = [] } = useQuery({
    queryKey: ['accounts'],
    queryFn: listAccounts,
    refetchInterval: 60_000,
  })

  useEffect(() => {
    if (accounts.length > 0 && accountId === 0) {
      setAccountId(accounts[0].id)
    }
  }, [accounts, accountId])

  return (
    <AccountContext.Provider value={{ accountId, setAccountId }}>
      <div className="flex h-screen bg-[#0f1117] text-slate-200 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-52 shrink-0 bg-[#131620] border-r border-slate-800 flex flex-col">
          <div className="px-4 py-4 border-b border-slate-800">
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-indigo-400" />
              <span className="text-sm font-semibold text-slate-100">Regime Trader</span>
            </div>
          </div>

          <div className="px-3 py-3 border-b border-slate-800">
            <label className="text-xs text-slate-500 block mb-1">Account</label>
            <select
              value={accountId}
              onChange={e => setAccountId(Number(e.target.value))}
              className="w-full bg-[#0f1117] text-slate-200 text-xs rounded px-2 py-1.5 border border-slate-700 focus:outline-none focus:border-indigo-500"
            >
              {accounts.length === 0 && <option value={0}>No accounts</option>}
              {accounts.map(a => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>

          <nav className="flex-1 p-2 space-y-0.5">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  clsx(
                    'flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
                    isActive
                      ? 'bg-indigo-600 text-white'
                      : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                  )
                }
              >
                <Icon className="w-4 h-4" />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="px-4 py-3 border-t border-slate-800 text-xs text-slate-600">
            TradingView → Tradovate
          </div>
        </aside>

        <main className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </AccountContext.Provider>
  )
}
