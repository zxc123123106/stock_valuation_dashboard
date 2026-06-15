# 股票估值統計看板

這是一個本機使用的 FastAPI + React 看板，用 SQLite 快取台股股票與 ETF 資料，提供買入價追蹤、估值比較與 Yahoo 主力進出資訊。

專案分成兩個本機服務：

- 後端 API：FastAPI + SQLite，預設執行於 `http://127.0.0.1:8000`
- 前端看板：React/Vite，預設執行於 `http://127.0.0.1:5173`，也可能使用 Vite 自動分配的其他 port，例如 `5174`

## 系統需求

- Python 3.13 或相容的 Python 3.x
- Node.js 24 LTS 或更新版本
- 可連線到 WantGoo、TWSE、HiStock 與 Yahoo 主力進出資料來源

## 安裝

建立 Python 虛擬環境並安裝後端依賴。

macOS / bash:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

Windows cmd:

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
pip install -r backend\requirements.txt
```

如果需要本機環境設定，可以複製範例檔。

macOS / bash:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

Windows cmd:

```bat
copy .env.example .env
copy frontend\.env.example frontend\.env
```

安裝前端依賴。

macOS / bash 與 Windows cmd:

```bash
cd frontend
npm install
```

## 啟動後端

在專案根目錄啟動後端。

macOS / bash:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Windows cmd:

```bat
.venv\Scripts\activate.bat
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

停止後端時，在同一個終端機按 `Control + C`。

後端會建立 `data/stock_valuation.sqlite3`。如果本機資料庫是空的，會先建立 `2330` 的初始範例資料。

## 啟動前端

另開一個終端機。

macOS / bash 與 Windows cmd:

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

開啟：

```text
http://127.0.0.1:5173/
```

如果 Vite 顯示其他可用 port，例如 `5174`，請開啟終端機顯示的實際網址。

## 健康檢查

macOS / bash:

```bash
curl http://127.0.0.1:8000/api/health
```

Windows cmd:

```bat
curl http://127.0.0.1:8000/api/health
```

正常會看到：

```json
{"status":"ok","app_env":"development","database":"sqlite","api_version":"0.1.0"}
```

## 手動更新

手動更新會把任務排入背景佇列，API 會立即回應，不會等待外部資料同步完成。

更新單一標的：

```bash
curl -X POST http://127.0.0.1:8000/api/stocks/2330/refresh
```

更新全部 active 標的：

```bash
curl -X POST http://127.0.0.1:8000/api/stocks/refresh
```

查看背景更新狀態：

```bash
curl http://127.0.0.1:8000/api/refresh/status
```

查看目前看板上的 active 標的：

```bash
curl http://127.0.0.1:8000/api/stocks
```

## 自動更新規則

前端每 5 秒讀取 SQLite 快取，不會等待外部 API，因此頁面不會因資料同步而整頁卡住。

後端自動更新只在台北時間平日 `09:00` 到 `14:00` 執行。

- 股票與 ETF 股價每 `BACKGROUND_REFRESH_SECONDS` 更新一次，預設為 `60` 秒。
- 主力進出每日更新一次，股票與 ETF 都會抓取。
- 本益比使用 TWSE OpenAPI，EPS 使用 HiStock，並在台北時間每日 `09:00` 後第一輪自動更新一次，只適用股票。
- ETF 顯示股價、買入價、每股未實現損益與主力進出；不顯示本益比、EPS 或估值列。
- `14:00` 後，後端會在每個平日做一次收盤補抓，確認最後一筆快取。
- 週末不會自動更新。
- 手動更新任何時間都可以使用。
- 看板右上角的更新按鈕會排入全部標的的全量更新，會重新抓股價、PE、EPS 與主力進出；個股卡片右上角的更新按鈕則維持該檔標的的一般背景更新。
- 更新失敗時會保留既有快取，並使用 1、3、5、15 分鐘的 backoff 節奏等待自動重試；手動更新會跳過等待時間。
- 後端啟動時會檢查 `crawler_logs` 清理狀態，預設每 24 小時清一次，保留最近 30 天紀錄。
- 目前不判斷台灣國定假日，因此平日假日仍可能嘗試更新。

如果外部資料來源暫時失敗，後端會盡量保留既有快取，並把失敗原因寫入 `crawler_logs`。

## 看板操作

- 在輸入框輸入代號並按 `加入/更新`，可以新增或更新標的。
- 每檔標的可以輸入買入價，用來顯示每股未實現損益。
- Toolbar 可選擇費率券商；目前支援國泰證券，設定會保存於 SQLite。
- 行情區顯示現價、開盤、昨收、當日最高與當日最低，並計算各指標相對現價的差距百分比。
- 按 `賣出` 會清除該檔標的目前買入價。
- 可以拖曳卡片左側排序把手調整順序，也可以用卡片右上角的上移/下移箭頭微調。
- 刪除標的是永久刪除，會移除該標的、持倉、股價快取、EPS、估值、主力進出與該標的更新紀錄。
- 重新加入同一個代號時，會建立新的本機快取。

現值估算公式為 `(估算股價 - 現價) / 現價 * 100`。成本估算公式為 `(估算股價 - 買入價) / 買入價 * 100`。純損益公式為 `(現價 - 買入價) / 買入價 * 100`。

國泰證券費後損益估算採上市櫃網路下單費率 `0.399‰`，同時估算買進與假設賣出的手續費。賣出端證券交易稅依資產類型計算：一般股票 `0.3%`、ETF `0.1%`。由於看板不保存股數，結果是每股估算，不包含成交總額取整、最低費用或個別優惠方案。

## API

- `GET /api/health`
- `GET /api/metadata`
- `GET /api/stocks`
- `GET /api/stocks/{symbol}`
- `GET /api/stocks/{symbol}/valuations`
- `GET /api/refresh/status`
- `GET /api/settings/broker`
- `PUT /api/settings/broker`
- `POST /api/stocks/refresh`
- `POST /api/stocks/{symbol}/refresh`
- `POST /api/stocks/reorder`
- `PUT /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}`

## 連接埠被佔用

查詢 port 使用狀態。

macOS / bash:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
```

Windows cmd:

```bat
netstat -ano | findstr :8000
netstat -ano | findstr :5173
```

確認 PID 是本專案舊的本機開發服務後，可以停止它。

macOS / bash:

```bash
kill <PID>
```

Windows cmd:

```bat
taskkill /PID <PID> /F
```

如果不確定該 port 是誰在使用，不要直接停止，先確認來源。

## 驗證

後端語法檢查。

macOS / bash:

```bash
source .venv/bin/activate
python -m compileall backend
```

Windows cmd:

```bat
.venv\Scripts\activate.bat
python -m compileall backend
```

前端 production build。

macOS / bash 與 Windows cmd:

```bash
cd frontend
npm run build
```

## 環境設定

已追蹤的範例檔：

- `.env.example`：後端與共用設定
- `frontend/.env.example`：Vite API base URL

本機忽略檔案：

- `.env`
- `frontend/.env`
- `.venv/`
- `data/*.sqlite3`

本機開發使用 SQLite。PostgreSQL、Docker、雲端部署與跨裝置 hosting 目前都不在範圍內。
