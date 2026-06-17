# frontend/ — React Dashboard

Vite + React + TypeScript dashboard that displays live trading data from the FastAPI backend. Dark-themed, single-page application with four routes.

## Toolchain

| Tool | Version | Purpose |
|------|---------|---------|
| Vite | 8.x | Dev server + build bundler |
| React | 19.x | UI framework |
| TypeScript | 6.x | Type safety |
| Tailwind CSS | 4.x (via `@tailwindcss/vite`) | Styling |
| TanStack Query | 5.x | Server state management |
| axios | 1.x | HTTP client |
| Recharts | 3.x | Equity curve chart |
| React Router | 7.x | Client-side routing |
| lucide-react | 1.x | Icons |

## Commands

```bash
npm run dev      # dev server at :5173 with proxy to :8000
npm run build    # TypeScript check + Vite build → frontend/dist/
npm run lint     # ESLint
npm run preview  # serve the built dist/ locally
```

## Dev proxy (vite.config.ts)

All three of these are proxied transparently to the backend during dev:
- `/api/*` → `http://localhost:8000`
- `/webhook/*` → `http://localhost:8000`
- `/ws` → `ws://localhost:8000` (WebSocket)

In production, FastAPI serves `frontend/dist/` directly at `/`.

## Source layout

See [src/CLAUDE.md](src/CLAUDE.md) for component-level detail.

```
frontend/src/
├── pages/          Dashboard, Trades, Strategies, Settings
├── hooks/          useWebSocket (auto-reconnect)
├── lib/            api.ts (typed axios), context.ts, format.ts
├── App.tsx         Root shell + routing + account selector
└── main.tsx        React/QueryClient entry point
```

## Build output

`npm run build` emits to `frontend/dist/`. FastAPI auto-mounts this at `/` when the directory exists (see `api/server.py:_FRONTEND_DIST`). **The dist/ files are committed** to the repo so the app can be served on a fresh clone without running `npm run build`.

## Sync rules

- **Add a new page** → add a route in `App.tsx`, add the page file under `src/pages/`, update `src/CLAUDE.md`.
- **Add a new npm dependency** → note it in the toolchain table above.
- **Change the Vite proxy targets** → update the dev proxy section.
- **Change the build output dir** → update both this file and `api/server.py:_FRONTEND_DIST`.
