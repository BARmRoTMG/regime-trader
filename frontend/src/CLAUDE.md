# frontend/src/ — Component Architecture

Component-by-component reference for the React dashboard. Read this when modifying any UI file.

## State management pattern

- **Server state:** TanStack Query (`useQuery`, `useMutation`, `useQueryClient`)
- **Real-time updates:** `useWebSocket` hook → on WS message → `queryClient.invalidateQueries()` (never update React state directly from WS)
- **Global account selection:** `AccountCtx` from `lib/context.ts`, accessed via `useAccount()` hook
- **Local UI state:** standard `useState` (pagination, filters, form inputs, modals)

## lib/

### lib/api.ts
Typed axios wrapper for every backend endpoint. Base URL: `/api`.

**TypeScript interfaces:**
```
Account        { id, name, broker, environment, notes, is_active, created_at }
OpenPosition   { trade_id, symbol, direction, contracts, entry_price, strategy_name,
                 regime_at_entry, unrealised_pnl }
Portfolio      { account_id, equity, cash, pnl, pnl_pct, regime, circuit_breaker,
                 open_positions }
Trade          { id, account_id, symbol, direction, contracts, entry_price, exit_price,
                 pnl, pnl_pct, duration_mins, opened_at, closed_at, strategy_name,
                 regime_at_entry, is_open, is_winner }
TradeSummary   { total_trades, winners, losers, win_rate, total_pnl, avg_winner,
                 avg_loser, max_loss, max_win }
EquityPoint    { recorded_at, equity }
Signal         { id, account_id, symbol, action, contracts, price, regime,
                 strategy_name, approved, rejection_reason, received_at }
Strategy       { id, name, description, is_enabled, created_at, last_signal }
```

**Functions:** `listAccounts`, `createAccount`, `updateAccount`, `deleteAccount`, `getPortfolio`, `listTrades`, `getTradeSummary`, `getEquityCurve`, `listSignals`, `listStrategies`, `updateStrategy`

### lib/context.ts
```typescript
AccountCtx { accountId: number, setAccountId: (id: number) => void }
```
Created in `App.tsx`, consumed via `useAccount()` in all pages.

### lib/format.ts
Shared display formatters:
| Function | Input | Output example |
|----------|-------|---------------|
| `fmtMoney(n)` | `1234.5` | `"$1,234.50"` (null → `"—"`) |
| `fmtPct(n)` | `0.0512` | `"5.1%"` |
| `fmtTime(iso)` | ISO string | `"14:32"` (HH:MM, 24h) |
| `fmtDate(iso)` | ISO string | `"Jun 18"` |

## hooks/

### hooks/useWebSocket.ts
Auto-reconnecting WebSocket hook.

```typescript
useWebSocket(handler: (msg: WsMessage) => void): void
// WsMessage = { type: 'signal' | 'equity' | 'ping' } & Record<string, unknown>
```

- Connects to `/ws` (auto-selects `ws://` or `wss://` from `window.location.protocol`)
- Ignores `ping` messages (filtered before handler is called)
- Reconnects after 3 s on disconnect
- Uses ref-based handler to avoid stale closure issues

## pages/

### pages/Dashboard.tsx
Live portfolio overview.

**Data sources:**
| Query | Endpoint | Refetch |
|-------|----------|---------|
| Portfolio | `GET /api/portfolio/{id}` | 30 s |
| Equity curve | `GET /api/trades/{id}/equity?limit=200` | 60 s |
| Signals feed | `GET /api/signals/{id}?limit=15` | 15 s |

**WebSocket:** On `type: 'signal'` or `type: 'equity'` → invalidates all three queries.

**UI sections:**
- 4-column stat cards: Strategy Equity, Session P&L, Regime badge, Circuit Breaker badge
- `regimeBadge(regime)` — color-coded: green (LOW_VOL), yellow (MID_VOL), red (HIGH_VOL), grey (null)
- `cbBadge(cb)` — color-coded: green (NONE), yellow (DAILY_REDUCE), orange (DAILY_HALT/WEEKLY_REDUCE), red (WEEKLY_HALT/PEAK_HALT)
- Recharts `AreaChart` for equity curve
- Signal feed: latest 15 signals, scrollable
- Open positions table: symbol, direction, contracts, entry price, unrealised P&L, regime

### pages/Trades.tsx
Historical trade list with filtering, pagination, and stats.

**Pagination:** 20 trades per page, offset-based.

**Filters:** symbol text input, filter toggle (all / winners only / losers only), `closed_only=true` always.

**Stats bar (6 columns):** win rate, total P&L, avg winner, avg loser, max win, max loss.

**Table columns:** ID, Symbol, Direction, Qty, Entry, Exit, P&L, P&L%, Regime, Date, Duration.

### pages/Strategies.tsx
Enable/disable registered Pine Script strategies.

- Lists all strategies from `GET /api/strategies` (refetch 30 s)
- Toggle switch calls `PATCH /api/strategies/{name}` with `{ is_enabled: boolean }`, then invalidates cache
- Shows `last_signal` timestamp for each strategy
- Strategies auto-register on first TradingView alert (users see them appear here)

### pages/Settings.tsx
Account management, webhook URL, and risk threshold reference.

**Sections:**
1. **Accounts** — list, create (name + demo/live selector), delete (with confirmation)
2. **Webhook URL** — computed as `http(s)://{hostname}:8000/webhook/alert`; copy-to-clipboard (grey → emerald feedback)
3. **Risk Thresholds** — read-only display of the 5 circuit-breaker levels (−2%, −3%, −5%, −7%, −10%)

## App.tsx
Root shell: sidebar + `<Outlet>` for page content.

- Sidebar: logo, 4 nav links (Dashboard, Past Trades, Strategies, Settings), account dropdown
- Account dropdown populates from `GET /api/accounts` (60 s refetch)
- `AccountCtx` provider wraps the app; initial `accountId = 0` until user selects

## Sync rules

- **Add a new API endpoint** → add a typed function to `lib/api.ts` and add the interface if a new response type is introduced.
- **Add a new page** → create under `pages/`, add route in `App.tsx`, add a nav link in the sidebar, document the data sources table here.
- **Add a formatter** → add to the `format.ts` table above.
- **Change refetch intervals** → update the Data sources table in the relevant page section.
- **Add a WS message type** → update `useWebSocket.ts` type union and add handling in Dashboard (or relevant page), then update `api/CLAUDE.md` message types table.
- **Change circuit-breaker display labels** → update `cbBadge()` section above AND `api/CLAUDE.md` thresholds table (they must match).
