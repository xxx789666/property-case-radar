# 台灣房地產案件雷達

共用基礎（FastAPI、PostgreSQL/SQLAlchemy、APScheduler、實價登錄共用模型
`market_prices`、Discord 互動行程）之上，包含兩條依 CLAUDE.md 保持獨立的
垂直線：

- **一般出售物件**：`crawlers/sale`、`scoring/sale_score.py`、
  `notifications/sale_notification.py`、`database/models/sale.py`、
  `apps/services/sale_pipeline.py`。
- **法拍屋案件**：`crawlers/auction`、`scoring/auction_score.py`、
  `notifications/auction_notification.py`、`database/models/auction.py`、
  `apps/services/auction_pipeline.py`（含拍次/狀態機）。法拍底價/得標價/
  實價登錄比對一律使用整數台幣（`*_twd`），與
  `market_prices.average_unit_price_twd` 同單位，避免萬元／元混用。

## 安全與合規

- 一般出售物件由 `SALE_CAPTURE_SCRIPT` 指向 591 公開售屋頁面的 Playwright
  擷取腳本；法拍來源則由 `AUCTION_CAPTURE_SCRIPT` 指向核准的 MOJ 擷取腳本。
  兩者都不登入、不解 CAPTCHA，也不繞過網站存取限制。
- `SaleCrawler`／`AuctionAnnouncementSource` 的合約禁止繞過登入、付費牆、
  CAPTCHA、反爬與速率限制。
- 新增真實 adapter 前，需先確認來源條款，只能存取核准的公開頁面/API，並保留
  隨機延遲、有限重試與可停用開關。
- 法拍案件的債務人／所有權人屬自然人資料，公開頻道一律遮罩（見
  `notifications/auction_masking.py`）；狀態更新備註（自由文字）公開頻道完
  全不顯示。所有 `/auction` 指令與通知函式在未指定 `audience` 時預設
  `"public"`（遮罩），需由呼叫端在確認為私人頻道後才明確傳入 `"private"`。
- `.env.example` 只有空白 token 欄位；請勿提交 `.env` 或真實 Discord token。

## 安裝與啟動

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item .env.example .env
docker compose up -d postgres
alembic upgrade head
uvicorn apps.api.main:app --reload
```

API：`GET /health`、`GET /api/v1/properties`、`GET /api/v1/properties/{id}`。
OpenAPI 位於 `/docs`。

排程器每日執行一次指定的 MOJ 法拍擷取腳本，解析其下載的官方案件明細 HTML，
再送入既有案件狀態、評分、資料庫與通知流程。預設路徑為
`D:\網頁識別認證\capture_auction_results.py`：

```powershell
python -m apps.scheduler.main
```

正式 Windows PostgreSQL 17 的資料目錄為 `D:\PostgreSQL\17\data`。
`C:\Program Files\PostgreSQL\17\data` 僅暫時保留為搬遷回復副本，不是目前
服務使用中的資料目錄。

實價基準每天同步內政部當期公開資料，並從官方季度下載介面快取最近12季
（3年）的土地交易至 `D:\網頁識別認證\moi_cache`。土地依行政區及建地、
農地、工業用地分組，當期與季度重疊交易會先去重再彙總及重新評分。

擷取腳本需要 Playwright、Chromium、`requests`，以及可連線的 Umi-OCR
（預設 `http://127.0.0.1:1224/api/ocr`）。排程會先檢查服務；若尚未啟動，
會在背景執行 `D:\Umi-OCR_Paddle_v2.1.5\Umi-OCR.exe`，等待 API 就緒後再抓取。
可用
`AUCTION_CAPTURE_ENABLED=false` 暫停法拍排程。

一般售屋排程每 24 小時執行一次
`scripts\capture_sale_results.py`，住宅抓取 22 縣市最新三頁、土地逐行政區抓取最新三頁公開物件並送入售屋
資料庫、評分及 Discord 通知流程。第一次基準匯入不推播既有物件；新上架頻道
每天只推播一則各縣市新增數量摘要，不逐案推播。降價與新出現的高分物件仍由
各自頻道通知。基準匯入或擴大抓取範圍補建的物件會標記為 `is_backfill`，
不計入每日新增，也不觸發新增、高分或個人訂閱通知；後續真實降價仍會通知。
來源有 `listed_date` 時依刊登日期統計，沒有刊登日期時才以 Radar 的
`first_seen_at` 計算。房地與法拍原始 JSON 預設各保留30天，成功抓取後自動刪除超期
JSON，不會刪除法拍 PDF、HTML 或資料庫內容。可用
`SALE_CAPTURE_ENABLED=false` 暫停此排程。
物件連續 3 天未在最新三頁再次出現時，排程會開啟其 591 公開原始網址驗證；
只有頁面明確顯示不存在、關閉或下架才標記為 `inactive`。網路錯誤、驗證頁或
無法辨識的回應不改狀態，歷史資料也不刪除。

租屋排程固定於台灣時間每日 11:00 執行
`scripts\capture_rental_results.py`，抓取 591 公開租屋頁面 22 縣市的一般
最新三頁及「其他」類型最新十頁（`kind=24`），也抓取 591 商用租賃的
店面、辦公、住辦及廠房最新三頁，並以來源與案件編號共同去重。591 的
店面與住辦多數歸在「其他」，因此不能只靠標題關鍵字取得。
另外，啟用中的租屋訂閱若指定行政區，系統會針對該行政區的各出租類型
額外抓取最新十頁，以涵蓋被全市大量刊登擠到較後頁的案件。
首輪資料標記為基準補建，不推播為今日新增；後續依
`source + source_property_id` 去重並保存租金歷史，推播每日各縣市新增統計、
降價案件及 80 分以上案件。高分案件先寫入耐久 outbox，每日只推播一則摘要，
列出最高分前 20 筆，其餘保留紀錄並可由 Bot 搜尋。連續三天未重新抓到的案件，需由公開明細頁確認
已下架後才標記為 `inactive`；連線或辨識失敗不會誤判下架。租屋 JSON 預設
保留 30 天。

Discord 自然語言查詢目前由 OpenAB 提供。使用者在限定頻道標記
`@Property Case Radar` 後，Bot 自動建立討論串並在串內回答；後續問題不必再次
標記。舊 `Property Case Radar Commands` 程序不再啟動，guild/global slash
commands 已清空。

```powershell
python scripts/bootstrap_openab_windows.py
powershell -ExecutionPolicy Bypass -File scripts/install_openab_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts/register_openab_tasks.ps1
```

OpenAB 僅接受私人搜尋頻道 `1530076451242508318`、`1530076529751756870`、
`1532282082854830202`
與設定中的使用者白名單。`Property Case Radar Discord Q&A Allowlist Sync`
每 5 分鐘檢查一次伺服器成員，只把已完成驗證並持有 `Radar 問答` 身分組的
成員同步到 OpenAB 使用者 ID 白名單。新成員起初只能看到 `#驗證`，閱讀規則
並點擊驗證訊息下方的 ✅ 後，排程會授予身分組並解鎖其他頻道；只有名單
變動才重啟 Gateway。資料庫存取使用
`radar_agent_ro` 專用唯讀角色；
每次查詢寫入 `logs/openab-query.jsonl`，gateway 與 sidecar 分別寫入
`logs/openab-gateway.log`、`logs/openab-sidecar.log`。Windows 登入後由
`Property Case Radar OpenAB Gateway` 與
`Property Case Radar OpenAB Sidecar` 排程自動啟動。

排程自癒器每 5 分鐘檢查 Scheduler、OpenAB Gateway／PDF Broker、
OpenAB Sidecar／訂閱 Broker及每日資料庫備份。常駐服務異常會限次重啟；
每日 crawler 或實價同步失敗則由 Scheduler 在 15 分鐘後自動補跑，最多 3 次。
修復成功或重試耗盡只通知 Discord `系統-log` 頻道。註冊方式：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_scheduler_self_healer_task.ps1
```

Umi-OCR 由獨立的登入排程持續管理。排程在登入後延遲 60 秒啟動，
異常退出時每分鐘自動重啟（最多 3 次）；自癒器只重啟此排程，不再產生
無人管理的 OCR 子程序：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_umi_ocr_task.ps1
```

排程直接持有 Umi-OCR 程序，避免隱藏 PowerShell 中介程序造成 GUI 程序立即
退出。PID、程序數、1224 埠及 Task Scheduler 退出碼會寫入
`logs/scheduler-self-healer.log`；Umi-OCR 本身的錯誤保存在安裝目錄下的
`UmiOCR-data/logs`。

一般售屋查詢請在 `1530076451242508318` 標記 Bot，例如：
`@Property Case Radar 幫我查詢桃園中壢區 1500 萬以內案件`。Agent 會固定轉成
`house-search --city 桃園市 --district 中壢區 --max-total-price-twd 15000000`
的唯讀資料庫查詢。
租屋查詢請在 `1532282082854830202` 標記 Bot，例如：
`@Property Case Radar 幫我查詢桃園市中壢區每月 3 萬元以內的兩房租屋`。
Agent 會使用獨立的 `rental-search` 唯讀查詢，不會混用出售案件。
同一頻道可說：
`@Property Case Radar 訂閱桃園市中壢區每月 3 萬元以內、有電梯的兩房租屋`。
租屋訂閱只通知建立訂閱後新抓到且符合條件的案件；可用
「列出我的訂閱」查看編號，或說「取消租屋訂閱 7」停用。
「住辦／店面」會查詢 591 一般租屋的「其他」出租類型及 591 商用租賃
的店面、辦公、廠房，不只依賴標題關鍵字；
結果可能同時包含其他非住宅用途，Bot 會在回覆中提示。
同一入口也支援公開土地資料；「土地／農地／建地」會分別轉成
`--property-type land / farmland / building_land`，並以土地坪數及土地單價呈現。
建地搜尋涵蓋住宅用地、商業用地、工業用地、住宅區、商業區、工業區，以及
建築基地、特定目的事業用地與甲／乙／丙／丁種建築用地；若指定
「甲建／乙建／丙建／丁建」，則使用對應的細分類型精確篩選。

土地使用獨立的 100 分評分，不套用住宅屋齡或車位條件：

- 低於內政部同區土地成交均價：50 分
- 同區土地成交流動性：25 分
- 掛牌降價幅度：10 分
- 土地資料完整度：15 分

若內政部目前批次沒有相同行政區的土地成交資料，土地案件保留「未評分」，不使用
住宅均價替代。

公告頻道由設定提供：

一般出售物件：每日各縣市新增統計 `1530073991442595880`（不逐案推播）、
降價 `1530075359490609202`、高分物件摘要 `1530075382873587855`
（每日一則、最高分前 20 筆）。

租屋案件：每日各縣市新增統計 `1532276339405492304`（不逐案推播）、
降價 `1532277198663319674`、高分案件摘要 `1532277201045684356`
（每日一則、最高分前 20 筆）。

法拍屋案件：每日各縣市新增統計 `1530075570140876982`（不逐案推播）、
每日各縣市二拍與三拍新增統計 `1530075673052577922`（分兩則、不逐案推播）、高分案件 `1530075701150089409`、停拍撤回
`1530075739750268989`。

## Legacy Commands Bot（已停用）

`apps.discord_bot` 與 `radarbot` Compose profile 保留作歷史相容程式碼，但目前
不建立程序、不建立 Windows 排程，也不註冊 `/house`、`/auction` 指令。查詢
統一從 `@Property Case Radar` 的 OpenAB 討論串進入。

The legacy `radarbot` Compose profile runs only the deterministic Discord
slash-command process when explicitly requested. It never starts
`apps.scheduler.main` or a crawler.
The container is non-root, read-only, capability-free, attached only to the
internal `radar-db-net` and its dedicated `radar-bot-egress`, and receives
both credentials as file-based Docker secrets:

- `openab/.local/discord_commands_bot_token`: the separate Commands bot token.
- `openab/.local/radarbot_database_url`: an internal
  `postgresql+psycopg://radar_bot_app:...@postgres:5432/radar` DSN.

Create/update `radar_bot_app` with
`deploy/radarbot/bootstrap_app_role.sql`, passing a cryptographically random
password through the required psql `app_password` variable, then write the
matching DSN directly to the gitignored secret file without printing it.
The role may read and mutate existing application tables/sequences but has
no database/schema CREATE or TEMP privilege; Alembic remains a separate
operator action.

```powershell
docker compose --profile radarbot build radarbot
docker compose --profile radarbot up -d radarbot
docker compose --profile radarbot ps
```

The service becomes healthy only after Discord login, successful guild
sync, and an exact local command contract check (`/house`: 6 subcommands,
`/auction`: 7 subcommands). The readiness record contains IDs/counts only,
never either secret.

## Production official-data scheduler

The opt-in `scheduler` profile is a separate hardened process. It downloads
the Ministry of the Interior's licensed current actual-price sales batch
from `https://plvr.land.moi.gov.tw/opendata/lvr_landAcsv.zip`, identifies
itself with a project User-Agent, limits requests to at most one per second,
uses bounded retries/timeouts/size and record limits, and revalidates its
private cache with ETag/Last-Modified. It aggregates current transactions into
`market_prices`; full transaction addresses are not persisted by this adapter.

The scheduler deliberately has no Discord secret, OpenAB/Codex state, Docker
socket, or host bind mount. It is attached only to the internal project DB
network and a dedicated HTTPS egress network:

```powershell
docker compose --profile scheduler build scheduler
docker compose --profile scheduler up -d scheduler
docker compose --profile scheduler ps
```

The host scheduler can additionally run the configured MOJ capture script.
The hardened Docker profile keeps that host-only integration disabled because
it has no D: drive mount, Chromium runtime, or route to the local Umi-OCR
process.

Judicial Yuan remains unused. The host integration uses the Ministry of
Justice Administrative Enforcement Agency source selected by the operator;
it does not substitute 591/Sinyi/private realtor data.

## 舊 `/house` 指令（程式碼保留，Discord 已取消註冊）

- `/house search`：城市、行政區、總價、坪數、屋齡、折價率篩選
- `/house subscribe`：建立使用者訂閱
- `/house latest`：最新物件
- `/house detail`：依資料庫 ID 查看物件
- `/house compare`：掛牌與區域行情比較
- `/house unsubscribe`：取消自己的訂閱

## 舊 `/auction` 指令（程式碼保留，Discord 已取消註冊）

- `/auction search`：縣市、行政區、類型、底價上限、拍次、點交篩選
- `/auction subscribe`：建立使用者訂閱
- `/auction latest`：最新法拍案件
- `/auction detail`：依法院＋案號查看案件（含債務人／所有權人，依頻道遮罩）
- `/auction schedule`：查看即將開標（未來 N 天內，排除流標與已終結案件）
- `/auction risk`：點交／產權風險評分
- `/auction unsubscribe`：取消自己的訂閱

## Discord LLM 互動層（目前正式入口）

`openab/` 提供以 OpenAB 0.10.0-beta.2 + Codex ACP 為基礎的自然語言問答層，
限定在兩個私人搜尋頻道，透過專用唯讀角色與
`tools/radar_agent_query.py` 讀取 Radar 資料庫。Windows 原生部署將 Discord
gateway 與持有資料庫 secret 的 ACP sidecar 分成兩個程序，僅以 loopback TCP
橋接；secret 與下載的執行檔均位於 gitignored 目錄。詳見 `openab/README.md`。

## 測試

```powershell
pytest
pytest tests/test_scoring.py
pytest tests/test_auction_scoring.py
```

測試使用 SQLite、fixture 與 Discord fake/mock，不會存取外部房仲網站或法院公告
網站，也不會連線 Discord。正式環境使用 PostgreSQL；SQLite 只供自動化測試。
