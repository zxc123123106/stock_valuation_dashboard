# 股票估值統計看板

這是一個本機使用的 FastAPI + React 看板，用 SQLite 快取台股股票與 ETF 資料，提供買入價追蹤、估值比較與 Yahoo 主力進出資訊。

專案分成兩個本機服務：

- 後端 API：FastAPI + SQLite，預設執行於 `http://127.0.0.1:8000`
- 前端看板：React/Vite，預設執行於 `http://127.0.0.1:5173`，也可能使用 Vite 自動分配的其他 port，例如 `5174`

## 系統需求

- Python 3.13 或相容的 Python 3.x
- Node.js 24 LTS 或更新版本
- 可連線到 TWSE、FinMind，以及使用者指定的 Yahoo 主力進出資料來源

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

`FINMIND_TOKEN` 為可選設定；未填寫時使用 FinMind 匿名額度，若日線更新頻繁或遇到額度限制，可在根目錄 `.env` 填入 token。

AI 分析使用後端環境變數，不會把 API key 放到前端。第一版建議使用 Gemini：

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=你的 Google AI Studio API key
GEMINI_MODEL=gemini-3.5-flash
```

如果要改用 OpenRouter：

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=你的 OpenRouter API key
OPENROUTER_MODEL=你選擇的模型名稱
```

修改 `.env` 後需要重啟後端才會生效。不要把 `.env` 提交到版本控制。

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

- 股票與 ETF 股價每 `BACKGROUND_REFRESH_SECONDS` 更新一次，預設為 `60` 秒；資料來源依序為 FinMind sponsor 即時快照、TWSE MIS、FinMind `TaiwanStockPrice` 最近收盤。
- 主力進出每日更新一次，股票與 ETF 都會抓取。
- 日線使用 FinMind `TaiwanStockPrice` 每日更新一次，股票與 ETF 都會保存最近約 600 個日曆日的歷史資料；盤中另以現價快取補上當日暫定 K 棒。
- 目前PE優先使用 TWSE OpenAPI；TWSE 無資料時 fallback 到 FinMind `TaiwanStockPER` 最新 PER。
- 近三年平均PE與PE區間使用 FinMind `TaiwanStockPER`。
- EPS 與季度基本面使用 FinMind `TaiwanStockFinancialStatements`，月營收使用 FinMind `TaiwanStockMonthRevenue`，並在台北時間每日 `09:00` 後第一輪自動更新一次，只適用股票。
- ETF 顯示股價、買入價、每股未實現損益與主力進出；不顯示目前PE、EPS、基本面或估值列。
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
- 股票卡片顯示目前PE、近三年平均PE與近三年PE區間。
- 股票卡片的「基本面」預設收合，展開後顯示 EPS、月營收、毛利率、營益率與淨利率。
- 每張卡片的「技術分析」預設收合，展開後顯示最近 120 個交易日的日 K、MA5/10/20/60/120/240、成交量摘要與十字線。
- 每張卡片右上角的 AI 圖示會開啟浮動分析面板。開啟面板只讀 SQLite 最新快取，不會呼叫外部 AI；按 `產生分析` 或 `更新分析` 才會消耗 API 額度。
- `未持有` 分析固定產生進場評估，結論限制為 `分批布局 / 等待 / 避開 / 資料不足`。存在成交均價時會再產生獨立的 `持有中` 分析，結論限制為 `續抱 / 觀察 / 分批調節 / 重新評估`。
- 未持有模式不傳成交均價或個人損益；持有中模式只傳成交均價與每股／百分比損益。兩種模式都不傳持股股數、總成本、持倉市值、資產規模或帳戶資訊。
- AI 分析使用 `v2-dual-mode` prompt。OpenRouter 會先要求 strict structured output；若免費 provider 因參數支援標記而無法路由，會自動降級為 `json_object`，但仍須通過後端完整格式與隱私驗證。驗證失敗的結果只保存為檢查 Log，不會成為成功快取。
- 按 `賣出` 會清除該檔標的目前買入價。
- 可以拖曳卡片左側排序把手調整順序，也可以用卡片右上角的上移/下移箭頭微調。
- 刪除標的是永久刪除，會移除該標的、持倉、股價快取、EPS、估值、主力進出與該標的更新紀錄。
- 重新加入同一個代號時，會建立新的本機快取。

現值估算公式為 `(估算股價 - 現價) / 現價 * 100`。成本估算公式為 `(估算股價 - 買入價) / 買入價 * 100`。純損益公式為 `(現價 - 買入價) / 買入價 * 100`。

國泰證券費後損益估算採完整的一買一賣模型：買入手續費為 `買入價 × 0.399‰`，賣出手續費為 `現價 × 0.399‰`，兩次手續費分別依當時價格計算。賣出時另扣證券交易稅：一般股票為 `現價 × 0.3%`、ETF 為 `現價 × 0.1%`。費後損益公式為 `現價 - 賣出手續費 - 交易稅 - 買入價 - 買入手續費`。由於看板不保存股數，結果是每股估算，不包含成交總額取整、最低費用或個別優惠方案。

## API

- `GET /api/health`
- `GET /api/metadata`
- `GET /api/stocks`
- `GET /api/stocks/{symbol}`
- `GET /api/stocks/{symbol}/technical-analysis?limit=120`
- `GET /api/stocks/{symbol}/valuations`
- `GET /api/stocks/{symbol}/ai-analysis/latest`
- `POST /api/stocks/{symbol}/ai-analysis`
- `GET /api/ai-analysis/logs/export?format=json|csv`
- `GET /api/refresh/status`
- `GET /api/settings/broker`
- `PUT /api/settings/broker`
- `POST /api/stocks/refresh`
- `POST /api/stocks/{symbol}/refresh`
- `POST /api/stocks/reorder`
- `PUT /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}`

AI Log 匯出可使用 `symbol`、`mode`、`provider`、`date_from`、`date_to` 與 `limit` 篩選；預設最多 1000 筆，最高 5000 筆。匯出內容包含模式、prompt 版本、輸入摘要、正規化回覆、原始模型回覆、provider metadata 與驗證錯誤，不包含 API key。

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
