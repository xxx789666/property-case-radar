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

## 測試

```powershell
pytest
pytest tests/test_scoring.py
pytest tests/test_auction_scoring.py
```

測試使用 SQLite、fixture 與 Discord fake/mock，不會存取外部房仲網站或法院公告
網站，也不會連線 Discord。正式環境使用 PostgreSQL；SQLite 只供自動化測試。
