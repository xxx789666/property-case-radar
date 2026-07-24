# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

This repository now contains the v1 implementation of both pipelines (sale-core and the auction/法拍屋 vertical). Use these commands:

- Install: `pip install -e .[dev]`
- Test all: `pytest`
- Test one file: `pytest tests/test_scoring.py` (or `tests/test_auction_scoring.py`, etc.)
- API: `uvicorn apps.api.main:app --reload`
- Scheduler: `python -m apps.scheduler.main`
- Migrations: `alembic upgrade head`
- Discord bot: `python -m apps.discord_bot.main` (requires a local `DISCORD_TOKEN`)

Files present:

- `taiwan_real_estate_radar.md` — the full system design spec (in Chinese). This is the authoritative source for architecture, data model, scoring logic, and Discord command design. Read it before implementing anything in this project.
- `discord 伺服器.txt` — the actual Discord server's channel names and channel IDs for the deployed bot (主伺服器頻道 1530072733818556538), covering both the public announcement channels and the private search channels. Treat these IDs as environment-specific configuration, not something to invent or change.

## Project purpose

A Discord bot system ("台灣房地產案件雷達" / Taiwan Real Estate Case Radar) that crawls Taiwan property listings, scores them, and pushes notifications to Discord. It is split into **two independent pipelines** that share infrastructure but not logic:

1. **一般出售物件 (regular for-sale listings)** — sourced from 591, Sinyi (信義房屋), House Cat, or other realtor platforms.
2. **法拍屋案件 (court foreclosure auction cases)** — sourced from court auction announcements.

### What's shared vs. independent (from the spec, section 十七)

Shared:
- PostgreSQL database
- 內政部實價登錄 (Ministry of Interior actual transaction price data) — used by both pipelines for regional price comparison via a common `market_prices` table
- The Discord bot itself
- Notification system
- User subscription mechanism
- Regional market pricing module

Independent per pipeline:
- Crawler sources
- Case data fields / DB tables (`properties*` vs `auction_cases*`)
- Scoring formulas
- Risk fields (auction-only: 點交/occupancy, 產權/title risk, 拍次/round number)
- Status tracking state machines
- Discord slash commands (`/house ...` vs `/auction ...`)

## Intended architecture (per the spec)

```
Discord Bot (search / subscribe / notify / admin)
        │
   ┌────┴────┐
   ▼         ▼
Sale line   Auction line
(591/Sinyi) (court filings)
   │            │
   ▼            ▼
PostgreSQL   PostgreSQL
   │            │
   ▼            ▼
compare vs   compare vs regional
market_prices market_prices
   │            │
   ▼            ▼
scoring      auction scoring
(discount %) (discount %, delivery/title risk)
   │            │
   ▼            ▼
Discord notify  Discord notify
```

### Planned tech stack (第一版/v1, spec section 十三)

Python, FastAPI, PostgreSQL, APScheduler, discord.py, Docker Compose. Crawling via httpx/BeautifulSoup/Playwright. Do not introduce Celery/Redis/RabbitMQ/Elasticsearch until case volume justifies it — the spec explicitly says APScheduler is sufficient for v1.

### Planned project structure (spec section 十四)

```
real-estate-radar/
├─ apps/{discord_bot,api,scheduler}/
├─ crawlers/{sale,auction,transaction}/
├─ scoring/{sale_score.py,auction_score.py}
├─ notifications/{sale_notification.py,auction_notification.py}
├─ database/{models,migrations,repositories}/
├─ tests/
├─ docker-compose.yml
├─ .env.example
└─ requirements.txt
```

Keep sale-pipeline and auction-pipeline code physically separate (separate modules/files under `crawlers/`, `scoring/`, `notifications/`) rather than merging them behind shared abstractions — the spec deliberately treats them as independent verticals sharing only DB/bot/notification infra.

## Key domain logic to preserve when implementing

- **Sale discount rate**: `1 - 掛牌單價 ÷ 區域成交單價` (listing unit price vs. regional actual-transaction unit price).
- **Auction surface discount rate**: `1 - 法拍底價單價 ÷ 區域成交單價` (auction floor unit price vs. regional actual-transaction unit price) — explicitly called a "surface" rate because it excludes taxes, arrears, renovation, eviction, and litigation costs (spec section 十六).
- Auction cases require a status history state machine (新增公告 → 更正公告 → 底價變更 → 拍賣日期變更 → 流標/轉下一拍 → 停拍/撤回/拍定/得標) — this is materially different from the sale pipeline's simpler price-history tracking.
- Auction results involving natural-person data (debtor/owner names) should not be broadcast in full to public Discord channels — spec section 七 flags this explicitly.
- Crawler etiquette per spec section 十六: respect site ToS, add randomized delay/retry, prefer public APIs/pages, never bypass login/paywalls/anti-bot mechanisms.
