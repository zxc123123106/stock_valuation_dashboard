# Stock Valuation Dashboard

Local-only FastAPI + React MVP for comparing current stock prices against estimates derived from EPS and current P/E, with simple buy-price tracking.

The app is still split into two local services:

- Backend API: FastAPI, SQLite, runs on `http://127.0.0.1:8000`
- Frontend dashboard: React/Vite, runs on `http://127.0.0.1:5173`

For day-to-day local operations, see [`docs/LOCAL_OPERATIONS.md`](docs/LOCAL_OPERATIONS.md). Backend startup and refresh operations are handled by the user locally.

## Local Setup

Node 24 LTS is recommended for the frontend. The project also accepts newer local Node versions:

```bash
nvm use
```

Create or activate the Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

Optional local environment files can be created from the examples when you are ready to run locally:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

Install frontend dependencies:

```bash
cd frontend
npm install
```

## Run

Backend:

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --reload
```

Frontend:

```bash
cd frontend
npm run dev
```

The backend creates `data/stock_valuation.sqlite3` and seeds a local snapshot for TSMC `2330` when the local database is empty.

Refresh stock or ETF data from the local backend. The backend updates SQLite by combining WantGoo quote data, TWSE OpenAPI P/E data, FinMind quarterly EPS data, and Yahoo broker-trading data:

```bash
curl -X POST http://127.0.0.1:8000/api/stocks/2330/refresh
```

The refresh API queues a background cache update and returns immediately. While the backend is running, active stock prices are refreshed in the background every 60 seconds, P/E is refreshed monthly, EPS is refreshed quarterly, broker trading is refreshed daily, and the frontend reads from SQLite cache.

ETF support is intentionally simple in this version: ETF cards track quote price, buy price, and unrealized per-share profit/loss, but they do not show P/E, EPS, valuation rows, or broker trading.

## API

- `GET /api/health`
- `GET /api/metadata`
- `GET /api/stocks`
- `GET /api/stocks/2330`
- `GET /api/stocks/2330/valuations`
- `GET /api/refresh/status`
- `POST /api/stocks/refresh`
- `POST /api/stocks/2330/refresh`
- `POST /api/stocks/reorder`
- `PUT /api/stocks/2330/position`
- `DELETE /api/stocks/2330/position`
- `DELETE /api/stocks/2330`

Deleting a stock permanently removes that symbol and its local SQLite cache rows. Re-adding the same symbol creates a fresh local cache.

## Environment

Tracked examples:

- `.env.example` for backend and shared settings
- `frontend/.env.example` for Vite settings

Ignored local files:

- `.env`
- `frontend/.env`
- `.venv/`
- `data/*.sqlite3`

Local development uses SQLite. PostgreSQL, Docker, cloud deployment, and cross-device hosting are intentionally out of scope.

Current EPS types are `TTM` and `LAST_YEAR`.

The dashboard shows current-value estimate as `(estimated price - current price) / current price * 100`; positive values mean the estimate is above the current price. Cost estimate uses `(estimated price - buy price) / buy price * 100`, and unrealized profit/loss uses `(current price - buy price) / buy price * 100`.
