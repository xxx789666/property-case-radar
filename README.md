# 台灣房地產案件雷達

共用基礎（FastAPI、PostgreSQL/SQLAlchemy、APScheduler、實價登錄共用模型
`market_prices`、Discord bot 行程）之上，包含兩條依 CLAUDE.md 保持獨立的
垂直線：

- **一般出售物件**：`crawlers/sale`、`scoring/sale_score.py`、
  `notifications/sale_notification.py`、`database/models/sale.py`、
  `apps/services/sale_pipeline.py`、Discord `/house` 指令。
- **法拍屋案件**：`crawlers/auction`、`scoring/auction_score.py`、
  `notifications/auction_notification.py`、`database/models/auction.py`、
  `apps/services/auction_pipeline.py`（含拍次/狀態機）、Discord `/auction`
  指令。法拍底價/得標價/實價登錄比對一律使用整數台幣（`*_twd`），與
  `market_prices.average_unit_price_twd` 同單位，避免萬元／元混用。

## 安全與合規

- `FixtureSaleCrawler`／`FixtureAuctionAnnouncementSource` 是預設且唯一可
  執行的來源，不會連線 591、信義房屋或任何法院公告網站。
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

排程器每 60–120 分鐘讀取一般出售物件 fixture、每 6–12 小時（預設 8 小時，對
應「每天 2～4 次」）讀取法拍公告 fixture：

```powershell
python -m apps.scheduler.main
```

Discord bot：

```powershell
$env:DISCORD_TOKEN = "僅放在本機環境，不要寫入檔案或版本庫"
python -m apps.discord_bot.main
```

Bot 只要求 guild 權限，不啟用 message content intent。單一 bot process 在
`setup_hook` 同時 `add_cog` HouseCog 與 AuctionCog，指令同步到
`1530072733818556538`。`/house` 查詢限制在私人頻道 `1530076451242508318`；
`/auction` 查詢限制在私人頻道 `1530076529751756870`。公告頻道由設定提供：

一般出售物件：新上架 `1530073991442595880`、降價 `1530075359490609202`、
高分物件 `1530075382873587855`。

法拍屋案件：新公告 `1530075570140876982`、即將開標 `1530075622636781639`、
二拍三拍 `1530075673052577922`、高分案件 `1530075701150089409`、停拍撤回
`1530075739750268989`。

本輪不執行 Discord live smoke。部署窗口可在只有一個 bot process 時設定 token
後執行上述 bot 指令，確認 `/house latest` 與 `/auction latest`。

## Production RadarBot container

The opt-in `radarbot` Compose profile runs only the deterministic Discord
slash-command process. It never starts `apps.scheduler.main` or a crawler.
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
only the Ministry of the Interior's licensed current actual-price sales batch
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

Live unattended auction crawling is currently fail-closed:

- Judicial Yuan `aomp109.judicial.gov.tw/robots.txt` returns
  `User-agent: *` and `Disallow: /`.
- The approved official Ministry of Justice Administrative Enforcement
  Agency replacement permits HTML in robots.txt, but `/Estate/Query` requires
  a CAPTCHA for every fresh query.

The code therefore does not solve/replay CAPTCHA values, submit controlled
queries, or substitute 591/Sinyi/private realtor data. Auction fixtures remain
test-only, and pending auction outbox rows are not drained by this scheduler
until a compliant official current-auction feed is available and reviewed.

## `/house` 指令

- `/house search`：城市、行政區、總價、坪數、屋齡、折價率篩選
- `/house subscribe`：建立使用者訂閱
- `/house latest`：最新物件
- `/house detail`：依資料庫 ID 查看物件
- `/house compare`：掛牌與區域行情比較
- `/house unsubscribe`：取消自己的訂閱

## `/auction` 指令

- `/auction search`：縣市、行政區、類型、底價上限、拍次、點交篩選
- `/auction subscribe`：建立使用者訂閱
- `/auction latest`：最新法拍案件
- `/auction detail`：依法院＋案號查看案件（含債務人／所有權人，依頻道遮罩）
- `/auction schedule`：查看即將開標（未來 N 天內，排除流標與已終結案件）
- `/auction risk`：點交／產權風險評分
- `/auction unsubscribe`：取消自己的訂閱

## Discord LLM 互動層（選用，預設不啟動）

`openab/` 提供以 OpenAB 0.10.0-beta.2 + Codex ACP 為基礎的自然語言問答層，
限定在 `/house`、`/auction search` 使用的同兩個私人頻道，讀取 Radar 資料庫
（唯讀，`tools/radar_agent_query.py`）。**必須使用第二個獨立 Discord bot／
token**，不得與本節上方的 `DISCORD_TOKEN`／RadarBot 共用（同一 token 開兩個
Gateway session 不是 Discord 支援的用法）。詳見 `openab/README.md`（架構、
token 邊界、頻道白名單、機密管理、版本鎖定、bootstrap 步驟、離線驗證指令）。
2026-07-24 的 bounded live Discord smoke 已確認獨立 bot 可登入，且 `/house`
與 `/auction` 指令同步成功；但頻道 `1530076529751756870` 缺少
`Send Messages` 權限，因此尚無法完成 in-channel 回覆驗證。此變更未修改
Discord 權限，也未將任何真實 token 寫入 repository。

## 測試

```powershell
pytest
pytest tests/test_scoring.py
pytest tests/test_auction_scoring.py
```

測試使用 SQLite、fixture 與 Discord fake/mock，不會存取外部房仲網站或法院公告
網站，也不會連線 Discord。正式環境使用 PostgreSQL；SQLite 只供自動化測試。
