# 台灣房地產案件雷達 — Sale Core v1

這個首版提供共用基礎與「一般出售物件」垂直線：FastAPI、PostgreSQL/SQLAlchemy、
APScheduler、離線 fixture crawler、實價登錄共用模型、折價/評分、通知，以及
Discord `/house` 指令。法拍 crawler、模型、評分與指令刻意不在本 worktree 實作。

## 安全與合規

- `FixtureSaleCrawler` 是預設且唯一可執行的來源，不會連線 591 或信義房屋。
- `SaleCrawler` 的合約禁止繞過登入、付費牆、CAPTCHA、反爬與速率限制。
- 新增真實 adapter 前，需先確認來源條款，只能存取核准的公開頁面/API，並保留
  隨機延遲、有限重試與可停用開關。
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

排程器目前每 60–120 分鐘讀取安全 fixture：

```powershell
python -m apps.scheduler.main
```

Discord bot：

```powershell
$env:DISCORD_TOKEN = "僅放在本機環境，不要寫入檔案或版本庫"
python -m apps.discord_bot.main
```

Bot 只要求 guild 權限，不啟用 message content intent。指令同步到
`1530072733818556538`，且 `/house` 查詢限制在私人頻道
`1530076451242508318`。公告頻道由設定提供：

- 新上架 `1530073991442595880`
- 降價 `1530075359490609202`
- 高分物件 `1530075382873587855`

本輪不執行 Discord live smoke，避免與 auction worker 同時連線。部署窗口可在只有
一個 bot process 時設定 token 後執行上述 bot 指令，確認 `/house latest`。

## `/house` 指令

- `/house search`：城市、行政區、總價、坪數、屋齡、折價率篩選
- `/house subscribe`：建立使用者訂閱
- `/house latest`：最新物件
- `/house detail`：依資料庫 ID 查看物件
- `/house compare`：掛牌與區域行情比較
- `/house unsubscribe`：取消自己的訂閱

## 測試

```powershell
pytest
pytest tests/test_scoring.py
```

測試使用 SQLite、fixture 與 Discord fake/mock，不會存取外部房仲網站，也不會連線
Discord。正式環境使用 PostgreSQL；SQLite 只供自動化測試。
