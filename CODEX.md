# CODEX.md

# 股票估值統計看板專案規格書

## 0. 目前開發範圍修正

本版專案改為只需在本機運行，不需要部署到雲端，也不需要提供其他裝置連線使用。

仍需維持前後端分離：

text 前端 React/Vite 本機服務 後端 FastAPI 本機 API SQLite 本機資料庫 

本版不規劃：

text 雲端部署 PostgreSQL Docker 跨裝置共享 本機以外的排程服務 

---

## 1. 專案名稱

股票估值統計看板 Stock Valuation Dashboard

---

## 2. 專案目標

本專案目標是建立一個簡易的股票估值統計看板，用於整理股票的即時股價、EPS、本益比與估值差異。

本系統不是交易系統，也不是股價預測系統，而是用來輔助觀察：

- 目前股價相對於不同 EPS 口徑是否偏高或偏低
- 不同 EPS 來源對估算股價的影響
- 同一檔股票在不同獲利基準下的估值差異
- 多檔股票的估值比較

---

## 3. 核心概念

系統會讀取股票的即時股價，並搭配 EPS 與目前本益比計算估算股價。

核心公式如下：

text 估算股價 = EPS × 目前本益比 

差異金額與百分比差異公式如下：

text 差異金額 = 即時股價 - 估算股價 百分比差異 = (即時股價 - 估算股價) / 即時股價 × 100% 

解讀方式：

text 百分比差異 > 0：即時股價高於估算股價 百分比差異 < 0：即時股價低於估算股價 百分比差異 ≈ 0：即時股價接近估算股價 

---

## 4. 專案定位

本專案定位為：

text 股票估值統計工具 

不包含以下功能：

text 不提供買賣建議 不進行自動交易 不預測未來股價 不保證資料完全即時 不保證估值結果代表合理價格 

本專案的重點是資料整理、估值比較與視覺化呈現。

---

## 5. 主要使用情境

使用者可以在網頁看板中查看一檔或多檔股票的估值統計資料。

以台積電 2330 為例，系統會顯示：

text 股票代號 股票名稱 即時股價 目前本益比 近四季 EPS / TTM EPS 去年全年 EPS 不同 EPS 口徑計算出的估算股價 即時股價與估算股價的百分比差異 資料更新時間 

---

## 6. 資料來源規劃

### 6.1 主要資料來源

本機版採用多來源整合，避免只解析單一網頁初始 HTML 而造成資料落後。

以台積電為例：

text https://www.wantgoo.com/stock/2330/financial-statements/eps 

系統需要從資料來源取得：

text 即時股價 目前本益比 近四季 EPS / TTM EPS 去年全年 EPS 

目前實作來源：

| 資料 | 來源 |
|---|---|
| 即時股價、股票名稱 | WantGoo quote JSON |
| 目前本益比 | TWSE OpenAPI BWIBBU_ALL |
| 季度 EPS、TTM EPS、去年全年 EPS | FinMind TaiwanStockFinancialStatements |

---

### 6.2 EPS 類型

系統需要支援以下 EPS 來源口徑：

| EPS 類型 | 說明 |
|---|---|
| TTM EPS / 近四季 EPS | 最近四季 EPS 加總 |
| 去年全年 EPS | 最近一個完整年度 EPS |

---

### 6.3 本益比來源

目前本益比優先採用證交所 OpenAPI 統計的個股本益比。

注意：

目前本益比可能已經是由資料來源依照某種 EPS 口徑計算得出。

因此若使用：

text TTM EPS × 目前本益比 

可能會接近目前股價。

這是正常現象，因為：

text 本益比 = 股價 / EPS 

本系統仍保留此計算，因為系統重點是比較不同 EPS 口徑下的估值差異。

---

## 7. 估值邏輯設計

同一檔股票應該可以產生多筆估值資料。

例如：

| 股票 | 即時股價 | EPS 類型 | EPS | 目前本益比 | 估算股價 | 百分比差異 |
|---|---:|---|---:|---:|---:|---:|
| 2330 | 2425 | TTM EPS | 74.39 | 32.00 | 2380.48 | 1.84% |
| 2330 | 2425 | 去年全年 EPS | 66.26 | 32.00 | 2120.32 | 12.56% |

以上數字僅為格式示意，不代表真實資料。

---

## 8. 建議資料欄位

### 8.1 股票基本欄位

text symbol：股票代號，例如 2330 name：股票名稱，例如 台積電 market：市場，例如 TWSE currency：幣別，例如 TWD 

---

### 8.2 價格欄位

text current_price：即時股價 price_updated_at：股價更新時間 

---

### 8.3 本益比欄位

text current_pe：目前本益比 pe_source：本益比來源 pe_updated_at：本益比更新時間 

---

### 8.4 EPS 欄位

text eps_type：EPS 類型 eps_value：EPS 數值 eps_period：EPS 對應期間 eps_source：EPS 來源 eps_updated_at：EPS 更新時間 

EPS 類型可使用：

text TTM LAST_YEAR 

---

### 8.5 計算結果欄位

text estimated_price：估算股價 price_difference：即時股價 - 估算股價 difference_percent：以即時股價為基準的百分比差異 valuation_status：估值狀態 calculated_at：計算時間 

---

## 9. 估值狀態規則

可以先使用以下規則：

| 百分比差異 | 狀態 |
|---:|---|
| 大於 10% | 高於估算 |
| 3% 到 10% | 略高於估算 |
| -3% 到 3% | 接近估算 |
| -10% 到 -3% | 略低於估算 |
| 小於 -10% | 低於估算 |

狀態命名可以先使用：

text OVERVALUED SLIGHTLY_OVERVALUED FAIR SLIGHTLY_UNDERVALUED UNDERVALUED 

---

## 10. 系統架構

本專案採用本機前後端分離架構：

text 使用者瀏覽器     ↓ React/Vite 前端本機服務 http://localhost:5173     ↓ FastAPI 後端本機 API http://localhost:8000     ↓ SQLite 本機資料庫 data/stock_valuation.sqlite3 

爬蟲或資料更新流程仍不應直接寫在前端。若之後加入爬蟲，應由後端或獨立本機腳本負責更新 SQLite 資料。

本機服務分工如下：

text 前端：顯示表格、篩選、排序、錯誤狀態 後端：提供 API、讀寫資料庫、估值計算 資料庫：保存股票資料、EPS、估值結果與錯誤紀錄 

---

## 11. 前端規格

### 11.1 前端目標

前端負責顯示股票估值統計資料。

前端不應負責主要爬蟲邏輯。

前端可以負責：

text 顯示股票群組 顯示估值差異 顯示統一資料更新時間 搜尋股票代號 拖曳調整股票順序 刪除或恢復標的 顯示錯誤或資料缺漏狀態 

---

### 11.2 前端本機開發工具

前端使用：

text React Vite npm 

本機啟動方式：

text cd frontend npm install npm run dev 

---

### 11.3 前端畫面建議

首頁看板採每檔股票一個群組，避免股票、股價、本益比在同一檔股票的多筆 EPS 估值列中重複出現。

首頁採深色主題，並支援每 60 秒由前端頁面自動更新目前顯示的 active 標的。

每檔股票群組標頭建議欄位：

| 欄位 | 說明 |
|---|---|
| 股票代號 | 例如 2330 |
| 股票名稱 | 例如 台積電 |
| 即時股價 | 目前股價 |
| 目前本益比 | 資料來源提供 |

群組內估值表建議欄位：

| 欄位 | 說明 |
|---|---|
| EPS 類型 | TTM、去年全年 |
| EPS | EPS 數值 |
| 估算股價 | EPS × 目前本益比 |
| 差異 | 百分比在上、價差在下，公式為即時股價 - 估算股價 |

資料時間統一顯示於總覽區，不在每檔股票卡片中重複顯示。

---

## 12. 後端 API 規格

### 12.1 後端目標

後端負責提供資料 API，供前端讀取。

後端不一定要即時請求資料來源。

建議做法是：

text 手動更新流程先把資料寫入資料庫 後端 API 再從資料庫讀取資料 前端只呼叫後端 API 

這樣可以避免每次使用者打開網頁時都觸發資料來源請求。

---

### 12.2 後端本機開發工具

後端使用：

text Python FastAPI Uvicorn SQLAlchemy SQLite 

本機啟動方式：

text source .venv/bin/activate uvicorn backend.app.main:app --reload 

---

### 12.3 API 功能建議

初期 API 可以包含：

text GET /api/stocks 取得所有 active 股票的群組估值資料  GET /api/stocks/{symbol} 取得單一股票群組估值資料  GET /api/stocks/{symbol}/valuations 取得單一股票不同 EPS 類型的估值資料  POST /api/stocks/{symbol}/refresh 從資料來源更新或恢復單一股票資料  POST /api/stocks/reorder 保存 active 股票顯示順序  DELETE /api/stocks/{symbol} 隱藏並保留單一股票資料  GET /api/health 檢查後端服務是否正常  GET /api/metadata 取得資料來源、更新時間、版本資訊 

---

## 13. 資料庫規格

### 13.1 資料庫用途

資料庫負責保存：

text 股票清單 最新股價 目前本益比 不同 EPS 口徑資料 計算後的估值結果 資料更新時間 資料更新狀態 錯誤紀錄 

---

### 13.2 資料庫工具

本版只使用本機 SQLite。

資料庫檔案：

text data/stock_valuation.sqlite3 

SQLite 檔案不納入 Git 追蹤，並由後端在本機首次啟動時建立與初始化。

---

### 13.3 建議資料表

#### stocks

保存股票基本資料。

text id symbol name market currency is_active display_order created_at updated_at 

---

#### stock_metrics

保存最新股價與本益比。

text id stock_id current_price current_pe price_updated_at pe_updated_at source created_at updated_at 

---

#### stock_eps

保存不同 EPS 口徑。

text id stock_id eps_type eps_value eps_period source eps_updated_at created_at updated_at 

---

#### stock_valuations

保存估值計算結果。

text id stock_id eps_type current_price current_pe eps_value estimated_price price_difference difference_percent valuation_status calculated_at created_at updated_at 

---

#### crawler_logs

保存爬蟲執行紀錄。

text id job_name status message started_at finished_at created_at 

---

## 14. 資料更新與排程規格

### 14.1 資料更新目標

資料更新流程負責從資料來源取得股票資料。

資料更新流程應該取得：

text 即時股價 目前本益比 TTM EPS 去年全年 EPS 資料更新時間 

---

### 14.2 本機更新流程候選

本版不使用雲端排程。

若需要更新資料，可先採用：

text 前端頁面每 60 秒自動呼叫後端 POST /api/stocks/{symbol}/refresh 前端 Refresh 按鈕呼叫後端 POST /api/stocks/{symbol}/refresh 手動執行本機更新 API macOS launchd 本機排程 

---

### 14.3 更新頻率

初期不需要秒級即時。

建議更新頻率：

text 股價與本益比：每 5 到 15 分鐘更新一次 EPS 資料：每日或每數小時更新一次 資料更新錯誤重試：失敗後記錄，不要無限重試 

由於 EPS 與本益比不是高頻交易資料，因此 MVP 階段可以接受一定程度延遲。

---

### 14.4 資料更新注意事項

資料更新流程需要注意：

text 網站結構可能改版 資料可能由 JavaScript 動態載入 請求過於頻繁可能被限制 資料可能有延遲 資料可能有缺漏 資料欄位可能出現文字、符號或空值 需要遵守資料來源網站的使用規範 

資料更新設計應避免：

text 過度頻繁請求 每次前端載入就觸發資料來源請求 沒有錯誤處理 沒有資料更新時間 沒有來源註記 

---

## 15. 資料清理規格

爬取到的資料需要先清理再寫入資料庫。

需要處理的格式包含：

text 千分位逗號，例如 2,380 倍數文字，例如 31.66倍 百分比符號，例如 8.18% 空白字串 null undefined N/A 日期格式 季度格式 

所有可計算欄位應轉換為數值型態。

所有資料應保留來源與更新時間。

---

## 16. 錯誤處理規格

系統需要處理以下錯誤：

text 資料來源網站無法連線 資料更新流程抓不到指定欄位 資料格式改變 EPS 缺漏 本益比缺漏 即時股價缺漏 資料庫寫入失敗 後端 API 無法讀取資料 前端 API 請求失敗 

錯誤處理原則：

text 不要讓整個系統因單一股票失敗而中斷 保留上一筆有效資料 顯示資料最後更新時間 記錄錯誤原因 前端顯示資料暫時無法更新 

---

## 17. MVP 階段規劃

### Phase 1：最小可行版本

目標：

text 完成一檔股票的估值統計 

範圍：

text 只支援台積電 2330 先使用本機 SQLite 示範資料 取得或寫入目前股價 取得或寫入目前本益比 取得至少一種 EPS 完成估算股價與百分比差異 前端用表格顯示結果 本機可啟動前端與後端 

---

### Phase 2：支援多種 EPS

目標：

text 同一檔股票支援 TTM EPS、去年全年 EPS 

範圍：

text 新增 EPS 類型欄位 同一檔股票產生多筆估值資料 前端可比較不同 EPS 口徑 

---

### Phase 3：支援多檔股票

目標：

text 支援多檔股票清單 

範圍：

text 股票清單由資料庫管理 資料更新流程根據股票清單更新資料 前端可以搜尋股票 前端可以依差異百分比排序 

---

### Phase 4：改善視覺化

目標：

text 讓估值差異更容易閱讀 

範圍：

text 加入顏色標示 加入估值狀態 加入篩選功能 加入資料更新時間提示 加入簡單圖表 

---

### Phase 5：加入備援資料來源

目標：

text 降低單一網站資料來源風險 

範圍：

text 加入第二資料來源 比對不同資料來源的數值 資料來源失敗時使用備援 顯示目前使用的資料來源 

---

## 18. 非功能性需求

### 18.1 可維護性

系統應該讓資料來源、計算邏輯、前端顯示分離。

不要把爬蟲、計算、顯示全部寫在同一個地方。

---

### 18.2 可擴充性

未來應能擴充：

text 更多股票 更多市場 更多估值方法 更多資料來源 更多圖表 更多篩選條件 

---

### 18.3 可靠性

系統應該：

text 保留最後一次成功資料 記錄資料更新錯誤 避免因資料缺漏導致整個頁面壞掉 在前端清楚顯示資料更新時間 

---

### 18.4 成本控制

本版只在本機運行，因此不產生雲端成本。

建議避免一開始加入 Docker、雲端資料庫或部署平台。

---

## 19. 建議技術組合候選

本專案不強制指定單一技術。

Codex 可依照實際開發環境選擇適合組合。

---

### 19.1 前端候選

text React Next.js Vue Nuxt SvelteKit 純 HTML + CSS + JavaScript 

若以學習與維護為主，推薦：

text React 或 Next.js 

若以最簡單 MVP 為主，推薦：

text 純 HTML + CSS + JavaScript 

---

### 19.2 後端候選

text Python FastAPI Python Flask Node.js Express Node.js NestJS 

若以資料處理與爬蟲方便性為主，推薦：

text Python FastAPI 

若以 JavaScript 全端一致性為主，推薦：

text Node.js Express 

---

### 19.3 資料更新候選

text requests + BeautifulSoup Playwright Selenium Puppeteer 

若資料可以直接從 JSON 或 API 取得，優先使用：

text requests 

若資料由 JavaScript 動態載入，再考慮：

text Playwright Selenium Puppeteer 

---

### 19.4 資料庫候選

text SQLite 

本機 MVP 使用：

text SQLite 

---

## 20. 本機執行組合

本版採用：

text 前端：React + Vite 後端：FastAPI + Uvicorn 資料庫：SQLite 虛擬環境：Python venv 套件管理：npm + pip 

本機啟動順序：

text 1. 建立或啟用 .venv 2. 安裝 backend/requirements.txt 3. 啟動 FastAPI 4. 安裝 frontend npm dependencies 5. 啟動 Vite 前端 

本版不需要雲端部署組合。

---

## 21. 前端顯示範例

首頁群組可設計如下：

text 2330 台積電 股價 2425 本益比 32.00 更新時間 2026-06-03 11:53

| EPS 類型 | EPS | 估算股價 | 差異 |
|---|---:|---:|---:|
| TTM | 74.39 | 2380.48 | +1.84% / +44.52 |
| LAST_YEAR | 66.26 | 2120.32 | +12.56% / +304.68 |

以上資料僅為畫面示意，不代表真實數字。

---

## 22. 重要限制與風險

### 22.1 目前本益比與 EPS 可能重複引用

如果網站目前本益比已經使用 TTM EPS 計算，則：

text TTM EPS × 目前本益比 

會接近即時股價。

這不代表系統錯誤，而是本益比公式本身造成的結果。

---

### 22.2 資料來源不穩定

資料來源 API、JSON 或網頁結構可能因網站改版失效。

因此需要：

text 錯誤紀錄 資料來源註記 最後成功資料保存 未來加入備援來源 

---

### 22.3 資料不是投資建議

前端需要顯示免責說明：

text 本看板僅用於資料整理與估值比較，不構成任何投資建議。 

---

## 23. Codex 開發原則

Codex 在開發本專案時，請遵守以下原則：

text 先完成 MVP，再擴充功能 前端、後端、資料更新、資料庫邏輯分離 不要把資料來源請求直接寫在前端 不要讓前端每次刷新都觸發資料來源請求 所有資料都要保留來源與更新時間 所有計算結果都要能追溯原始欄位 對資料缺漏要有錯誤處理 對資料更新失敗要保留 log 不要在初期過度設計 

---

## 24. 初期完成標準

第一版完成時，至少需要達成：

text 可以在本機開啟前端網站 前端可以從本機後端 API 取得資料 後端可以讀取本機 SQLite 資料庫 後端可初始化台積電示範資料 系統可以顯示台積電的即時股價、目前本益比、EPS、估算股價、百分比差異 資料群組可以正確顯示更新時間 API 失敗時不會讓整個前端壞掉 可輸入股票代號新增或更新本機 SQLite 內的標的 

---

## 25. 未來可擴充功能

未來可以加入：

text 自選股清單 多檔股票比較 歷史估值趨勢圖 本益比歷史區間 股價與估算股價走勢圖 高估 / 低估排序 資料來源切換 使用者登入 通知功能 LINE Bot / Telegram Bot 查詢 手機版優化 PWA 

---

## 26. 總結

本專案採用前後端分離架構，並透過本機資料更新流程取得股票資料。

核心邏輯是：

text 即時股價 vs EPS × 目前本益比 

系統會針對不同 EPS 口徑計算估算股價與百分比差異，讓使用者快速比較目前股價在不同獲利基準下的估值狀態。

此專案第一階段應以台積電 2330 作為 MVP 測試標的，確認資料取得、計算、API、資料庫與前端顯示流程皆可正常運作後，再擴充至多檔股票與更多資料來源。
