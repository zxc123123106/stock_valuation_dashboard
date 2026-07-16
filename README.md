# 股票估值統計看板

這是一個本機使用的 FastAPI + React 看板，用 SQLite 快取台股股票與 ETF 資料，提供買入價追蹤、估值比較與 Yahoo 主力進出資訊。

專案分成兩個本機服務：

- 後端 API：FastAPI + SQLite，預設執行於 `http://127.0.0.1:8000`
- 前端看板：React/Vite，預設執行於 `http://127.0.0.1:5173`，也可能使用 Vite 自動分配的其他 port，例如 `5174`

## 專案架構

專案採模組化單體架構：FastAPI、React 與 SQLite 仍在同一個 repository 中執行，但 HTTP、use case、資料存取、外部資料源與背景排程已分開管理。

後端主要目錄：

```text
backend/app/
  api/           FastAPI routers 與 HTTP 錯誤轉換
  services/      股票、基本面、技術面、AI、可信度等 use cases
  repositories/ SQLite 查詢與 transaction 操作
  providers/     TWSE、FinMind、Yahoo、TAIFEX、AI provider adapters
  db/            session、models、bootstrap 與快取寫入
  refresh/       manager、scheduler、job models 與四個更新通道
  schema/        依領域拆分的 Pydantic schemas
```

前端主要目錄：

```text
frontend/src/
  api/           API client 與領域端點
  hooks/         polling、排序、股票操作、AI 與可信度狀態
  components/    Dashboard、股票、基本面、技術、AI、WTX 等元件
  utils/         格式化與純函式
  styles/        tokens、base、layout 與各功能樣式
```

`backend/app/main.py` 只保留 app factory 入口，因此既有啟動方式 `uvicorn backend.app.main:app` 不變。`database.py`、`schemas.py`、`market_data.py` 與 `refresh_worker.py` 暫時保留為相容 facade，既有腳本可繼續匯入；新程式應使用對應的模組化套件。

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
AI_PROVIDER_ORDER=openrouter,gemini
GEMINI_API_KEY=你的 Google AI Studio API key
GEMINI_MODEL=gemini-3.5-flash
```

如果要改用 OpenRouter：

```env
AI_PROVIDER=openrouter
AI_PROVIDER_ORDER=openrouter,gemini
OPENROUTER_API_KEY=你的 OpenRouter API key
OPENROUTER_MODEL=你選擇的免費模型名稱:free
```

免費模式只接受以 `:free` 結尾的 OpenRouter 模型 ID，避免誤用付費端點。修改 `.env` 後需要重啟後端才會生效。不要把 `.env` 提交到版本控制。

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

後端啟動時會先自動執行 Alembic migration，再啟動背景更新 worker。既有資料庫第一次導入 Alembic 時，系統會先建立 `pre-migration` 一致性備份；migration 或完整性檢查失敗時，後端不會帶著不確定的 schema 繼續啟動。

## SQLite、Migration 與備份

每個後端 SQLite connection 都會啟用：

- WAL journal mode
- `busy_timeout=5000`
- `foreign_keys=ON`
- 股票關聯資料的 `ON DELETE CASCADE`

Alembic 預設會在後端啟動時自動升級。也可以在後端停止時手動檢查或升級：

macOS / bash:

```bash
source .venv/bin/activate
alembic current
alembic upgrade head
```

Windows cmd:

```bat
.venv\Scripts\activate.bat
alembic current
alembic upgrade head
```

資料庫每天台北時間 `03:00` 使用 SQLite online backup API 建立一致性備份；若後端當時未開啟，會在下一次啟動時補做。預設保留最近 14 份，位置是 `data/backups/`。可在 `.env` 調整：

```env
DATABASE_BACKUP_DIR=./data/backups
DATABASE_BACKUP_RETENTION_COUNT=14
DATABASE_BACKUP_HOUR=3
```

前端 Toolbar 的「資料管理」可立即備份、下載完整 SQLite，以及匯出／匯入使用者 JSON。使用者 JSON 只包含券商、追蹤清單、順序與成交均價，不包含 API key、行情快取或 AI log。匯入會先顯示新增、保留、刪除與持倉變更，再於確認後建立 `pre-import` 備份並取代目前使用者資料。

### 完整還原 SQLite

完整資料庫不接受從網頁上傳還原。請先停止後端，再保留目前檔案並放回下載的備份。

macOS / bash:

```bash
mv data/stock_valuation.sqlite3 data/stock_valuation.before-restore.sqlite3
cp data/backups/<備份檔名>.sqlite3 data/stock_valuation.sqlite3
```

Windows cmd:

```bat
move data\stock_valuation.sqlite3 data\stock_valuation.before-restore.sqlite3
copy data\backups\<備份檔名>.sqlite3 data\stock_valuation.sqlite3
```

完成後重新啟動後端；啟動流程會再次驗證 migration revision、外鍵與資料庫完整性。

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

後端採 24 小時分流排程，不會每 60 秒對所有標的執行完整刷新。行情、基本面、主力與歷史資料使用獨立 queue，Yahoo 或 FinMind 的慢請求不會阻塞行情更新。

- 股票與 ETF 行情在平日 `09:00-13:30` 每 `QUOTE_MARKET_INTERVAL_SECONDS` 更新一次，預設 `60` 秒；盤外與週末每 `QUOTE_OFF_HOURS_INTERVAL_SECONDS` 確認一次，預設 `900` 秒。
- 行情資料來源依序為 FinMind sponsor 即時快照、TWSE MIS、FinMind `TaiwanStockPrice` 最近收盤。TWSE MIS 最新成交價缺漏時使用最佳買價、再使用最佳賣價，並跳過漲跌停委託簿中的 `0` 哨兵值；絕不以開盤價代替現價。盤外日線 fallback 不比既有行情新時只確認來源可用，不覆蓋現價快取。
- 新增標的時優先使用 FinMind `TaiwanStockInfo` 辨識名稱與市場；若 FinMind 無法使用，後端會改查 TWSE MIS 的上市與上櫃頻道，自動辨識 `TWSE / TPEX`，避免上櫃標的被錯誤送往上市行情端點。
- 主力進出在平日 `18:10` 後更新，股票與 ETF 都會抓取；失敗時只重試主力通道。
- 日線與三年 PE 歷史在平日 `18:05` 後更新。日線使用 FinMind `TaiwanStockPrice`，股票與 ETF 都會保存最近約 600 個日曆日的歷史資料；盤中另以現價快取補上當日暫定 K 棒。
- 目前PE會比較 TWSE OpenAPI 與 FinMind `TaiwanStockPER` 的實際交易日期，採用日期較新的資料；同一交易日才優先使用 TWSE。

- 近三年平均PE與PE區間使用 FinMind `TaiwanStockPER`。
- EPS 與季度基本面在平日 `18:20` 後共用一次 FinMind `TaiwanStockFinancialStatements` 請求；月營收同時使用 FinMind `TaiwanStockMonthRevenue` 更新，只適用股票。
- 每月 `8-12 日` 的 `09:00-23:00`，月營收每 `MONTHLY_REVENUE_RELEASE_INTERVAL_SECONDS` 檢查一次，預設每 2 小時；抓到前一月份後停止加密查詢。
- ETF 顯示股價、買入價、每股未實現損益與主力進出；不顯示目前PE、EPS、基本面或估值列。
- 每個平日 `18:00` 後，目前 PE 每 `PE_POLL_INTERVAL_SECONDS` 輪詢一次，預設 15 分鐘，直到資料日期追上當日官方交易日；18:00 前只要求前一交易日資料。
- 週末只維持盤外行情確認；其他類別只在缺資料、過期或前次失敗尚未補齊時重試。
- 台指期 WTX 在日盤 `08:45-13:45`、夜盤 `15:00-05:00` 每 `FUTURES_REFRESH_SECONDS` 更新一次，預設 10 秒；休盤停止外部請求並顯示最近快取。
- 手動更新任何時間都可以使用。
- 看板右上角的更新按鈕會把全部標的的所有適用通道排入高優先 queue；個股卡片更新會立即更新行情，其餘只排入該檔目前到期或缺失的類別。
- 更新失敗時會保留既有快取，並使用 1、3、5、15 分鐘的 backoff 節奏等待自動重試；手動更新會跳過等待時間。
- 後端啟動時會檢查 `crawler_logs` 清理狀態，預設每 24 小時清一次，保留最近 30 天紀錄。
- 目前不判斷台灣國定假日，因此平日假日仍可能嘗試更新。

如果外部資料來源暫時失敗，後端會盡量保留既有快取，並把失敗原因寫入 `crawler_logs`。

## 資料可信度

每張標的卡片右上角的資料庫圖示會顯示目前資料品質；點擊後可分別查看行情、PE、基本面、籌碼、技術日線與 AI 分析。

- `即時 / 最新 / 延遲 / 過期 / 待更新 / 不適用` 表示資料本身的新鮮度。
- `使用快取` 是獨立狀態，代表最近一次同步失敗但仍有上次成功資料可用。
- 面板會分開顯示資料日期、後端取得時間、來源、最近錯誤與下次重試時間。
- ETF 的 PE 與基本面會顯示不適用，不列入整體品質判斷。
- 同一標的中單一資料來源失敗不會阻塞其他資料，錯誤會寫入 `data_refresh:{symbol}:{category}` crawler log。

## 看板操作

- 在輸入框輸入代號並按 `加入/更新`，可以新增或更新標的。
- 每檔標的可以輸入買入價，用來顯示每股未實現損益。
- Toolbar 可選擇費率券商；目前支援國泰證券，設定會保存於 SQLite。
- 行情區顯示現價、開盤、昨收、當日最高與當日最低，並計算各指標相對現價的差距百分比。
- 股票卡片顯示目前PE、近三年平均PE與近三年PE區間。
- 股票卡片的「基本面」預設收合，展開後顯示 EPS、月營收、毛利率、營益率與淨利率。
- 每張卡片的「技術分析」預設收合，展開後顯示最近 120 個交易日的日 K、MA5/10/20/60/120/240、成交量摘要與十字線。
- 每張卡片右上角的 AI 圖示會開啟浮動分析面板。開啟面板只讀 SQLite 最新快取，不會呼叫外部 AI；按 `產生分析` 或 `更新分析` 才會消耗 API 額度。
- `未持有` 分析固定產生進場評估；存在成交均價時，同一次模型請求會一併產生 `持有中` 解讀。批次 JSON 只有單一模式不合格時，才針對該模式補送一次請求。
- 最終狀態由版本化本機規則決定：未持有為 `分批布局 / 等待 / 避開 / 資料不足`，持有中為 `續抱 / 觀察 / 分批調節 / 重新評估`。AI 只負責解釋，不可改寫狀態。
- 唯一禁止傳送的個人部位資料是持有股數；持有中模式可使用成交均價、每股／百分比損益與其他公開或衍生指標。
- `AI_PROVIDER_ORDER` 控制免費 provider 切換順序。遇到 `429/502/503` 會寫入持久化 cooldown 並自動切換下一個 provider；全部不可用時仍立即顯示本機規則分析。
- 每次分析保存實際使用的正規化資料快照、來源日期與過期項目。AI 面板會分開顯示分析完成時間與各類資料截至時間。
- AI 分析使用 evidence-based prompt，回覆中的每個結論需引用後端提供的 evidence key。OpenRouter 會先要求 strict structured output；若免費 provider 因參數支援標記而無法路由，會降級為 `json_object`，但仍須通過完整格式驗證。
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
- `GET /api/data-management/status`
- `GET /api/data-management/backups`
- `POST /api/data-management/backups`
- `GET /api/data-management/backups/{filename}`
- `GET /api/data-management/export`
- `POST /api/data-management/import/preview`
- `POST /api/data-management/import`
- `POST /api/stocks/refresh`
- `POST /api/stocks/{symbol}/refresh`
- `POST /api/stocks/reorder`
- `PUT /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}/position`
- `DELETE /api/stocks/{symbol}`

AI Log 匯出可使用 `symbol`、`mode`、`provider`、`date_from`、`date_to` 與 `limit` 篩選；預設最多 1000 筆，最高 5000 筆。匯出內容包含模式、prompt／規則版本、分析 run、資料快照、正規化回覆、原始模型回覆、provider metadata 與驗證錯誤，不包含 API key。`GET /api/ai-analysis/provider-health` 可檢查各 provider/model 的 cooldown 狀態。

## 連接埠被佔用

查詢 port 使用狀態。

macOS / bash:

```bash
lslsof -nP -iTCP:5173 -sTCP:LISTEN
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

後端測試與語法檢查。

macOS / bash:

```bash
source .venv/bin/activate
python -m unittest discover backend/tests
python -m compileall backend
```

Windows cmd:

```bat
.venv\Scripts\activate.bat
python -m unittest discover backend\tests
python -m compileall backend
```

前端自動化測試與 production build。

macOS / bash 與 Windows cmd:

```bash
cd frontend
npm run test
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
- `data/backups/`

本機開發使用 SQLite。當追蹤標的長期超過約 50～100 檔或開始支援多使用者時，再評估 PostgreSQL；Docker、雲端部署與跨裝置 hosting 目前不在範圍內。
