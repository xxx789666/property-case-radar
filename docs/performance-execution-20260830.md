# 優化清單執行紀錄（2026-08-30）

## 完成狀態

1. 階段計時：已加入 capture、parse、ingest、score、verify、retry、Discord 的 `performance stage` 記錄。
2. 失敗重試：已改用 APScheduler `date` 工作；不再阻塞每日主工作（保留無 scheduler 測試相容路徑）。
3. 驗證併行：單一 Chromium、最多 4 分頁，按站點限速。
4. 591-business：狀態驗證上限降至 50，避免 99.8% unknown 拖慢任務。
5. Playwright：阻擋 image/media/font，保留 document/script/XHR/fetch。
6. 既有物件：一次 preload，取消逐筆 SELECT。
7. 寫入：PostgreSQL 原生 `INSERT ... ON CONFLICT DO UPDATE` 批次 upsert；SQLite/測試使用 ORM fallback，並維持單次 flush。
8. MarketPrice：一次載入 map。
9. 租屋行政區中位數：一次計算後以 map 重用。
10. workers：rental/sale 已以 6 workers 實測，22/22 城市成功、0 失敗；`.env` 已套用 6。
11. Cloudflare A/B：20 個公開 URL，結果在 `tmp/cloudflare-browser-run-ab.json`。
12. 導入判斷：Kitesurf 16/20 成功、完整率 proxy 12/20（60%，p50 6.50 秒、p95 10.80 秒、估算 $0.0030）；預設 Chromium 4/20、完整率 4/20（20%，p50 0.30 秒、p95 0.39 秒、估算 $0.0002）。完整率以回應 >=200 bytes 作保守 proxy，差異受來源頁面拒絕影響，暫不替換 591。

## 執行輸出

- Rental：`tmp/worker6-rental-focus/`，22/22 成功。
- Sale：`tmp/worker6-sale/`，22/22 成功、0 失敗。
- Cloudflare：`tmp/cloudflare-browser-run-ab.json`。
- Playwright runtime：`D:\property-case-radar\.runtime\playwright`（未寫入 C 槽）。

## Rust 判斷

目前主要耗時是網路、瀏覽器等待與站點限速，不是 CPU 解析；因此先不重寫 Rust。若後續 stage profiling 顯示 HTML 解析佔比成為主要瓶頸，再評估 LOL HTML/Pingora 等 Rust 元件。

## 套用注意

程式與 `.env` 已更新；scheduler 已於 2026-08-30 重啟並確認使用 `D:\property-case-radar\.runtime\python312`。

## 下一階段：瀏覽器與狀態驗證優化

執行順序：

1. 建立可重用 Chromium pool，先確認資料量與成功率不變。
2. 加入 HTTP 快速預檢，只有無法判定的頁面才升級到瀏覽器。
3. 依 `rent.591.com.tw`、`sale.591.com.tw`、`business.591.com.tw` 分開限速。
4. 加入 24 小時狀態快取，避免重複驗證相同 URL。
5. 僅在成功率與 challenge 穩定後評估將驗證分頁提高到 6。

每一步均需記錄：資料量、active/inactive/unknown、challenge、p50/p95、錯誤率與回退結果。

### 2026-08-30 延伸執行結果

- Chromium pool：狀態驗證採單一 browser、可重用 context/page；已通過 capture/verify 回歸測試。
- HTTP 預檢：已加入；僅 404/410 等明確下架回應直接判定 inactive，其餘一律升級瀏覽器，避免誤判。
- 網域限速：已分開 `rent.591.com.tw`、`sale.591.com.tw`、`business.591.com.tw` 的 token bucket。
- 24 小時快取：已加入 `sale_status_cache.json`、`rental_status_cache.json`，過期或損壞會安全回退重新驗證。
- 6 分頁評估：程式與 parser 已支援 6，但尚未啟用正式設定；需先取得至少一個完整日的 challenge/錯誤率資料。

## 法拍專屬優化（2026-08-30）

- 案件 HTML 重用：案件目錄已有有效 HTML 時不重新載入詳細頁。
- PDF 下載快取：以 `_pdf_urls.json` 保存 PDF URL 到本機檔案的對應；檔案存在且有效時跳過下載。
- OCR 文字快取：PDF 同目錄建立 `.txt` sidecar，PDF 未更新時直接讀文字，不重複 `PdfReader` 解析。
- 保守策略：未改動法拍縣市併發與 OCR worker 數，避免法院站點、Umi-OCR 資源競爭；先觀察完整日誌再評估併行下載。
- 驗收：`tests/test_captured_auction_source.py`、`tests/test_auction_scheduler.py` 共 15 項通過。

## Canary 與 staging 執行結果

- 新 URL manifest canary：D 槽單縣市、1 頁完成，產生 2 個案件 manifest 與 PDF，位於 `tmp/auction-manifest-canary-downloads/`。
- Staging PostgreSQL：因 `radar` 帳號沒有 CREATEDB 權限，已在 D 槽 PostgreSQL 建立隔離 schema `radar_staging`；未接觸正式表。
- Bulk smoke test：staging 內以同一案件先 ORM 建立，再以 PostgreSQL `ON CONFLICT DO UPDATE` 更新，結果正確為 `corrected`，測試資料已清除。
- 正式切換：尚未切換。完整法拍狀態歷史／回合／文件一致性仍需以 staging 匯入實際批次後再驗證。
