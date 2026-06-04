# 本機操作文檔

這份文件記錄本專案在 Mac 本機開發與操作的基本流程。後端服務由你自行啟動、停止與更新；Codex 不再代為啟動或重啟後端。

## 1. 啟動後端

在專案根目錄執行：

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

看到以下類似訊息代表後端已啟動：

```text
Uvicorn running on http://127.0.0.1:8000
```

停止後端時，在同一個終端機按 `Control + C`。

## 2. 啟動前端

另開一個終端機，在專案根目錄執行：

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

瀏覽器開啟：

```text
http://127.0.0.1:5173/
```

停止前端時，在同一個終端機按 `Control + C`。

## 3. 何時需要安裝依賴

第一次建立 Python 虛擬環境，或 `backend/requirements.txt` 有變更時：

```bash
source .venv/bin/activate
pip install -r backend/requirements.txt
```

第一次建立前端專案，或 `frontend/package.json` / `frontend/package-lock.json` 有變更時：

```bash
cd frontend
npm install
```

## 4. 檢查後端狀態

後端啟動後，可以在另一個終端機執行：

```bash
curl http://127.0.0.1:8000/api/health
```

正常會看到：

```json
{"status":"ok","app_env":"development","database":"sqlite","api_version":"0.1.0"}
```

## 5. 手動更新股票資料

更新單一標的。API 會立即回應並把標的排入背景快取更新，不會等待外部資料同步完成：

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

## 6. 自動更新規則

後端啟動後會先顯示 SQLite 既有快取，並在背景更新目前 active 標的。股價每 60 秒更新，PE 每月更新，EPS 每季更新；PE/EPS 來源暫時失敗時會沿用既有快取，不會阻止股價刷新。前端每 5 秒讀取快取與背景更新狀態，因此頁面不會因等待外部 API 同步而整頁卡住。

注意：

- 關閉前端頁面後，後端仍會在服務啟動期間維持背景快取更新。
- 後端未啟動時，前端會顯示 API 錯誤。
- 自動更新不會啟動後端；後端仍需你手動啟動。

## 7. 排序與刪除

拖曳股票卡片左側的排序圖示可以調整顯示順序，或使用卡片右上角的上移/下移箭頭微調排序。順序會保存到 SQLite。

刪除標的是永久刪除：

- 標的不會顯示在看板上。
- SQLite 內會刪除該標的、持倉、股價快取、EPS、估值與該標的更新紀錄。
- 重新輸入同一個股票代號並按「加入/更新」後，會建立全新的本機快取。

## 8. 連接埠被佔用

如果後端 8000 或前端 5173 被佔用，可以查詢佔用程序：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
```

確認是本專案的舊開發服務後，可以停止該 PID：

```bash
kill <PID>
```

若不確定該程序用途，不要直接 kill，先確認來源。

## 9. 靜態驗證

後端語法檢查：

```bash
source .venv/bin/activate
python -m compileall backend
```

前端 production build：

```bash
cd frontend
npm run build
```
