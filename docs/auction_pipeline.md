# 法拍屋 (Auction) Pipeline

Independent vertical implementing the auction half of
`taiwan_real_estate_radar.md`. Lives entirely under
`src/property_case_radar/auction/`, mirroring the layout the sale
pipeline (task A) is expected to use under `src/property_case_radar/sale/`
(see repo `CLAUDE.md`). Nothing in this vertical imports from a `sale`
package or vice versa; the only cross-pipeline dependency is the shared
market-price contract in `src/property_case_radar/shared/`.

## Module map

| Module | Responsibility |
| --- | --- |
| `auction/models.py` | Domain dataclasses: `AuctionCase`, `AuctionRound`, `AuctionDocument`, `AuctionStatusEvent`, `AuctionSubscription`, `AuctionFilter`. Maps 1:1 onto the `auction_*` tables in spec section 十一. |
| `auction/repository.py` | `AuctionRepository` protocol + `InMemoryAuctionRepository` reference implementation. |
| `auction/state_machine.py` | Status transition table + `apply_transition()` implementing spec section 九 (新增公告/更正公告/底價變更/拍賣日期變更/流標→轉下一拍/停拍/撤回/拍定). |
| `auction/scoring.py` | `surface_discount_rate()` (spec 八) + full investment-score breakdown (`score_case`) + `risk_level()`/`risk_score_0_100()`. |
| `auction/masking.py` | Natural-person name masking (spec 七's PII note) + `view_for_audience()`, the single choke point every renderer uses to decide public vs. private display. |
| `auction/parsers/` | `AnnouncementParser` protocol + `CourtAnnouncementParser`, a reference parser for a normalized fixture HTML format (not any specific real court site). |
| `auction/crawler/` | `AnnouncementSource` protocol + `FixtureCourtAnnouncementSource` (local-file only, zero network calls) + `RateLimiter` for a future live source. |
| `auction/fixtures/*.html` | Synthetic (hand-written, not scraped) announcement pages covering 新公告/更正公告/底價變更/拍賣日期變更. |
| `auction/notifications.py` | Builds the Discord message text for new-case and status-update notifications (spec 六/九 formats), plus public-channel routing helpers. |
| `auction/channels.py` | The real 法拍案件-* channel IDs from `discord 伺服器.txt`, tagged public/private. |
| `auction/discord/commands.py` | `/auction search|subscribe|latest|detail|schedule|risk|unsubscribe`, implemented as plain, framework-agnostic methods (no `discord.py` import). |
| `shared/market_prices.py` | `MarketPriceProvider` protocol + `InMemoryMarketPriceProvider`, the minimal shared `market_prices` contract. |

## Why fixtures, not a live crawl

Per `taiwan_real_estate_radar.md` section 十六 and this round's task
scope, no outbound network call is made anywhere in this vertical.
`FixtureCourtAnnouncementSource` reads local HTML files; tests run fully
offline. `RateLimiter` (randomized delay + exponential backoff) is
provided for whenever a live source is added, so that work starts from
an already-tested "polite crawler" primitive instead of inventing one
under time pressure.

A real implementation must, before going live:

- confirm the target court site's terms of service permit automated access,
- use only public announcement pages (no login bypass, no anti-bot evasion),
- add per-court field-mapping (each 地方法院's page differs; don't grow
  `CourtAnnouncementParser` into a many-branch guesser -- add a sibling
  module per court instead, as noted in that module's docstring).

## Why no live Discord connection

`discord/commands.py` never imports `discord.py`. Each `/auction ...`
handler is a plain method that takes typed arguments and a repository,
and returns a `CommandResponse(text, audience)`. This keeps the command
logic testable with an in-memory repository and fake market-price
provider (see `tests/auction/test_commands.py`) without a bot process,
a gateway connection, or real channel IDs. Wiring a real `discord.py`
`app_commands.Group` that calls into `AuctionCommandHandlers` and posts
the returned text via `interaction.response.send_message(...)` is a thin
adapter left for whichever task wires up the actual bot process
(`apps/discord_bot` per spec section 十四) -- it should not need to
change anything in this module, only call it.

## Integration points for the shared DB / bot layer (not built yet)

No shared PostgreSQL/ORM layer exists in this repository as of this
round (neither pipeline has landed `database/`). Two integration
seams are defined so that landing one doesn't require touching auction
business logic:

1. **`property_case_radar.auction.repository.AuctionRepository`** --
   any storage backend (SQLAlchemy + PostgreSQL, an HTTP client to a
   shared service, ...) just needs to implement `add_case`, `get_case`,
   `get_case_by_number`, `list_cases`, `add_subscription`,
   `get_subscription`, `list_subscriptions`, `remove_subscription`. Swap
   `InMemoryAuctionRepository` for the real implementation in whatever
   composes the scheduler/bot process; `discord/commands.py` and
   `notifications.py` are unaffected.
2. **`property_case_radar.shared.market_prices.MarketPriceProvider`** --
   the read contract for the shared `market_prices` table (fed by 內政部
   實價登錄, used by both pipelines per spec section 十一). Needs one
   method: `get_regional_average(city, district) -> RegionalMarketPrice | None`.

Ingestion glue -- turning a stream of `ParsedAnnouncement` (from
`parsers/`) into `AuctionCase` creation/updates and
`state_machine.apply_transition()` calls, then persisting via
`AuctionRepository` and pushing through `notifications.py` -- is
intentionally not implemented in this round. That's a scheduler-app
concern (`apps/scheduler` per spec section 十四) that depends on the
real repository existing; building it against the in-memory repository
now would just be thrown away.

## Compliance / PII notes carried into the code

- Every renderer that can reach a public channel takes an explicit
  `audience: Literal["public", "private"]` and routes through
  `masking.view_for_audience()`. The push notification format
  (`build_new_case_notification`, matching spec 六's example exactly)
  never includes 債務人/所有權人 at all. `/auction detail` and
  `/auction risk` show more (via `build_case_detail_text`), including
  debtor/owner, and mask them when `audience="public"`.
- **Fail-safe default**: `CommandResponse`, every `AuctionCommandHandlers`
  method, and `build_case_detail_text` default `audience` to `"public"`,
  not `"private"`. A framework adapter that forgets to pass `audience`
  therefore gets the masked/PII-free rendering, not a leak -- callers
  must positively opt in with `audience="private"` once they know
  they're in the private 法拍案件-搜尋 channel.
- `AuctionStatusEvent.note` (free text, e.g. "債務人王小明對本次公告提出
  異議") is only ever rendered for `audience="private"`. Unlike the
  structured 債務人/所有權人 fields, free text can't be reliably
  auto-redacted, so `build_status_update_notification` simply omits it
  entirely on public audiences rather than attempting partial masking.
- `scoring.SURFACE_DISCOUNT_DISCLAIMER` (spec 十六: 表面折價率不等於實際
  獲利...) is appended to every public-audience new-case notification.
- `/auction schedule`, `notifications._is_upcoming`, and the public
  channel-routing helpers (`channels_for_new_case`/
  `channels_for_status_event`) all exclude cases whose status is outside
  `state_machine.ACTIVE_STATES` (i.e. FAILED and every terminal status --
  SUSPENDED/WITHDRAWN/AWARDED): a case that's already resolved or
  between rounds has no valid "still going to auction on this date" to
  report. FAILED/AWARDED status-change events themselves still generate
  a notification (via `channels_for_status_event`), but route
  conservatively -- neither has a dedicated public channel in
  `discord 伺服器.txt`, so FAILED only posts to 二拍三拍 once the case has
  actually reached round 2+, and AWARDED posts to no public channel at
  all rather than being mislabeled into one that implies the case is
  still biddable.

## Invariants enforced by `state_machine.apply_transition`

- `changed_at` must not precede `case.updated_at` -- status history is
  append-only and moves forward in time only.
- Advancing rounds (`next_round` on a FAILED -> ANNOUNCED transition)
  requires `next_round.round_number` to be strictly greater than the
  round it replaces.
- `AuctionRound.__post_init__` rejects non-positive `round_number`,
  `floor_price_total`, `floor_unit_price`; a negative `deposit`; and a
  non-positive `winning_price` if one is supplied at construction time.
- Transitioning to AWARDED requires a positive `winning_price`; supplying
  `winning_price` for any other `to_status` is rejected.
- Transitioning to PRICE_CHANGED requires at least one of
  `new_floor_price_total`/`new_floor_unit_price`; DATE_CHANGED requires
  `new_auction_date`. Both are applied to `case.current_round` in place,
  and the previous/new values are captured on the returned
  `AuctionStatusEvent` so `build_status_update_notification` can render
  what changed. These payload kwargs are rejected outside their matching
  `to_status`.
- `state_machine._assert_invariants` runs at the end of every successful
  `apply_transition` call and raises `AssertionError` if any non-current
  round was left `RoundResult.PENDING`, or if the case is AWARDED without
  its current round carrying `RoundResult.AWARDED` and a positive
  `winning_price` -- a defensive check exercised directly in
  `tests/auction/test_state_machine.py::TestInvariantGuard`.

## Ownership data: unknown is not full

`OwnershipType.UNKNOWN` exists because an unparsed/missing 產權 field
must never be treated as confirmed clean title. `AuctionCase.ownership_type`
defaults to `UNKNOWN` (not `FULL`), `CourtAnnouncementParser` falls back
to `UNKNOWN` for a missing or unrecognized 產權 label, and
`scoring.ownership_score` gives `OwnershipType.UNKNOWN` a conservative
8/20 -- strictly below FULL (20) and above PARTIAL_SHARE (6), the same
"don't know = assume some risk" treatment already used for
`OccupancyStatus.UNKNOWN`.

## Running the tests

From this worktree root:

```bash
python -m pip install -r requirements.txt
python -m pytest
```

`pyproject.toml`'s `[tool.pytest.ini_options]` adds `src/` to
`sys.path`, so no `pip install -e .` is required. 115 tests cover
models (including `AuctionRound`'s positivity/round-number invariants),
the state machine (the full round-1→round-3→awarded flow, the
PRICE_CHANGED/DATE_CHANGED payload, and negative tests for every
rejected input -- backwards `changed_at`, non-advancing round numbers,
missing/non-positive `winning_price`, missing change payloads, and the
`_assert_invariants` guard itself), scoring (including the spec's own
25/40萬 worked example and the UNKNOWN-ownership conservative score),
masking, the fixture parser (all four announcement kinds plus missing/
unrecognized 產權 defaulting to UNKNOWN), the fixture-backed crawler
source, notification formatting/routing (including public-audience note
suppression and FAILED/AWARDED/terminal-status channel routing), and all
seven `/auction` command handlers (including the public-safe default
audience and `/auction schedule` excluding terminal/FAILED cases).

## Known gaps / explicitly out of scope this round

- No live crawler implementation (by design; see above).
- No live Discord bot process / gateway connection (by design; see above).
- No PostgreSQL-backed repository or ORM models (blocked on a shared
  database layer landing; the `AuctionRepository` protocol is ready for it).
- No ingestion pipeline wiring parser output → state machine → repository
  → notifications end-to-end (depends on the repository/scheduler above).
- No subscription-matching engine that proactively notifies subscribers
  when a new case matches their `AuctionFilter` (the filter's `matches()`
  is exercised by `/auction search`; wiring it into an ingestion pipeline
  is future work).
