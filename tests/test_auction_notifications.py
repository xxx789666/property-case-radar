from datetime import date, datetime, timezone

import pytest

from apps.services.auction_pipeline import apply_status_transition
from database.models.auction import AuctionCase, AuctionRound, AuctionStatus
from notifications import auction_notification as an
from notifications.auction_notification import (
    AuctionNotificationRouter,
    DeliveryOutcome,
    build_case_detail_text,
    build_new_case_notification,
    build_status_update_notification,
)
from scoring.auction_score import score_auction_case


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def sample_case() -> AuctionCase:
    return AuctionCase(
        court_name="桃園地方法院",
        case_number="115年度司執字第XXXX號",
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        city="桃園市",
        district="中壢區",
        debtor="王小明",
        owner="王小明",
        rounds=[
            AuctionRound(
                round_number=2, floor_price_total_twd=9_800_000, floor_unit_price_twd=232_000, auction_date=date(2026, 8, 18)
            )
        ],
    )


def test_build_new_case_notification_format(sample_case: AuctionCase) -> None:
    score = score_auction_case(sample_case, 358_000)
    result = build_new_case_notification(sample_case, 358_000, score, audience="public")
    assert result.text.startswith("⚖️ 新增法拍案件")
    assert "法院：桃園地方法院" in result.text
    assert "底價：980 萬" in result.text
    assert "表面折價率：35.2%" in result.text
    assert "點交：未知" in result.text  # occupancy_status defaults to UNKNOWN
    assert "拍賣日期：2026-08-18" in result.text
    # never leaks debtor/owner, masked or not, in the push notification
    assert "王" not in result.text


def test_build_new_case_notification_public_includes_disclaimer(sample_case: AuctionCase) -> None:
    score = score_auction_case(sample_case, 358_000)
    public = build_new_case_notification(sample_case, 358_000, score, audience="public")
    private = build_new_case_notification(sample_case, 358_000, score, audience="private")
    assert "表面折價率不等於實際獲利" in public.text
    assert "表面折價率不等於實際獲利" not in private.text


def test_build_case_detail_text_masks_by_audience(sample_case: AuctionCase) -> None:
    score = score_auction_case(sample_case, 358_000)
    public = build_case_detail_text(sample_case, 358_000, score, audience="public")
    private = build_case_detail_text(sample_case, 358_000, score, audience="private")
    assert "債務人：王○○" in public.text
    assert "債務人：王小明" in private.text


def test_build_case_detail_text_occupancy_note_hidden_for_public_chinese_name(sample_case: AuctionCase) -> None:
    sample_case.occupancy_note = "占用人王小明拒絕遷讓"
    score = score_auction_case(sample_case, 358_000)
    public = build_case_detail_text(sample_case, 358_000, score, audience="public")
    private = build_case_detail_text(sample_case, 358_000, score, audience="private")
    assert "王小明" not in public.text
    assert "占用情況" not in public.text
    assert "占用情況：占用人王小明拒絕遷讓" in private.text


def test_build_case_detail_text_occupancy_note_hidden_for_public_english_name(sample_case: AuctionCase) -> None:
    sample_case.occupancy_note = "Occupant John Smith refuses to vacate"
    score = score_auction_case(sample_case, 358_000)
    public = build_case_detail_text(sample_case, 358_000, score, audience="public")
    private = build_case_detail_text(sample_case, 358_000, score, audience="private")
    assert "John Smith" not in public.text
    assert "占用情況" not in public.text
    assert "John Smith" in private.text


def test_build_case_detail_text_default_audience_is_public() -> None:
    from database.models.auction import CaseType, OccupancyStatus, OwnershipType

    case = AuctionCase(
        court_name="c",
        case_number="n",
        first_seen_at=utc(2026, 1, 1),
        debtor="王小明",
        owner="王小明",
        rounds=[AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10)],
    )
    score = score_auction_case(case, 100)
    result = build_case_detail_text(case, 100, score)
    assert result.audience == "public"
    assert "王小明" not in result.text


def test_status_update_note_hidden_for_public_shown_for_private(sample_case: AuctionCase) -> None:
    event = apply_status_transition(
        sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 10), note="債務人王小明對本次公告提出異議"
    )
    public = build_status_update_notification(sample_case, event, audience="public")
    private = build_status_update_notification(sample_case, event, audience="private")
    assert "王小明" not in public.text
    assert "備註" not in public.text
    assert "備註：債務人王小明對本次公告提出異議" in private.text


def test_status_update_renders_price_change_payload(sample_case: AuctionCase) -> None:
    event = apply_status_transition(
        sample_case,
        AuctionStatus.PRICE_CHANGED,
        changed_at=utc(2026, 8, 10),
        new_floor_price_total_twd=7_840_000,
        new_floor_unit_price_twd=185_000,
    )
    result = build_status_update_notification(sample_case, event, audience="public")
    assert "底價：980 萬 → 784 萬" in result.text
    assert "底價單價：23.2 萬／坪 → 18.5 萬／坪" in result.text


def test_status_update_renders_date_change_payload(sample_case: AuctionCase) -> None:
    event = apply_status_transition(
        sample_case, AuctionStatus.DATE_CHANGED, changed_at=utc(2026, 8, 10), new_auction_date=date(2026, 9, 29)
    )
    result = build_status_update_notification(sample_case, event, audience="public")
    assert "拍賣日期：2026-08-18 → 2026-09-29" in result.text


class TestChannelRouting:
    """Exercises the module-level routing helpers directly (channels_for_new_case /
    channels_for_status_event / _is_upcoming), independent of AuctionNotificationRouter's
    mock-channel plumbing tested below.
    """

    def test_new_case_excludes_terminal_and_failed_status(self, sample_case: AuctionCase) -> None:
        score = score_auction_case(sample_case, 358_000)
        for status in (AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED, AuctionStatus.FAILED):
            sample_case.status = status
            assert an.channels_for_new_case(sample_case, score) == []
            assert an._is_upcoming(sample_case, within_days=7) is False

    def test_new_case_active_status_routes_normally(self, sample_case: AuctionCase) -> None:
        score = score_auction_case(sample_case, 358_000)
        targets = an.channels_for_new_case(sample_case, score)
        assert "new" in targets
        assert "round" in targets  # round_number == 2

    def test_status_event_suspended_goes_only_to_suspended_channel(self, sample_case: AuctionCase) -> None:
        event = apply_status_transition(sample_case, AuctionStatus.WITHDRAWN, changed_at=utc(2026, 8, 10))
        assert an.channels_for_status_event(sample_case, event) == ["suspended_withdrawn"]

    def test_status_event_awarded_has_no_channel(self, sample_case: AuctionCase) -> None:
        event = apply_status_transition(
            sample_case, AuctionStatus.AWARDED, changed_at=utc(2026, 8, 10), winning_price_twd=8_200_000
        )
        assert an.channels_for_status_event(sample_case, event) == []

    def test_status_event_failed_routes_to_round_only_at_round_2_plus(self, sample_case: AuctionCase) -> None:
        # sample_case starts at round 2
        event = apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 10))
        assert an.channels_for_status_event(sample_case, event) == ["round"]


@pytest.mark.asyncio
class TestAuctionNotificationRouter:
    async def test_publish_new_case_routes_second_round_and_high_score(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        score = score_auction_case(sample_case, 358_000)
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
            high_score_threshold=80,
        )
        outcomes = await router.publish_new_case(sample_case, 358_000, score)
        assert all(o.success for o in outcomes)
        kinds = {o.kind for o in outcomes}
        assert "new" in kinds
        assert "round" in kinds  # round_number == 2
        router.new_channel.send.assert_awaited_once()
        router.round_channel.send.assert_awaited_once()

    async def test_publish_new_case_skips_inactive_case(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        sample_case.status = AuctionStatus.AWARDED
        score = score_auction_case(sample_case, 358_000)
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        kinds = await router.publish_new_case(sample_case, 358_000, score)
        assert kinds == []
        router.new_channel.send.assert_not_called()

    async def test_one_destination_failing_does_not_block_the_others(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        score = score_auction_case(sample_case, 358_000)
        new_channel = AsyncMock()
        new_channel.send = AsyncMock(side_effect=RuntimeError("channel unavailable"))
        round_channel = AsyncMock()
        router = AuctionNotificationRouter(
            new_channel=new_channel,
            upcoming_channel=AsyncMock(),
            round_channel=round_channel,
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
            high_score_threshold=80,
        )
        # sample_case is round 2 -> routes to both "new" (fails) and
        # "round" (should still succeed despite "new" failing first).
        outcomes = await router.publish_new_case(sample_case, 358_000, score)
        by_kind = {o.kind: o for o in outcomes}
        assert by_kind["new"].success is False
        assert "channel unavailable" in by_kind["new"].error
        assert by_kind["round"].success is True
        assert by_kind["round"].error is None
        round_channel.send.assert_awaited_once()

    async def test_send_new_case_and_send_status_event_never_raise(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        score = score_auction_case(sample_case, 358_000)
        broken_channel = AsyncMock()
        broken_channel.send = AsyncMock(side_effect=RuntimeError("boom"))
        router = AuctionNotificationRouter(
            new_channel=broken_channel,
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        outcome = await router.send_new_case("new", sample_case, 358_000, score)
        assert outcome == DeliveryOutcome(kind="new", success=False, error="boom")

    async def test_publish_status_event_suspended_routes_only_to_suspended_channel(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        event = apply_status_transition(sample_case, AuctionStatus.WITHDRAWN, changed_at=utc(2026, 8, 10))
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        outcomes = await router.publish_status_event(sample_case, event)
        assert [o.kind for o in outcomes] == ["suspended_withdrawn"]
        assert outcomes[0].success
        router.suspended_channel.send.assert_awaited_once()
        router.new_channel.send.assert_not_called()

    async def test_publish_status_event_awarded_has_no_channel(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        event = apply_status_transition(
            sample_case, AuctionStatus.AWARDED, changed_at=utc(2026, 8, 10), winning_price_twd=8_200_000
        )
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        kinds = await router.publish_status_event(sample_case, event)
        assert kinds == []

    async def test_publish_status_event_failed_at_round_2_routes_to_round_channel(self, sample_case: AuctionCase) -> None:
        from unittest.mock import AsyncMock

        event = apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 10))
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        outcomes = await router.publish_status_event(sample_case, event)
        assert [o.kind for o in outcomes] == ["round"]
        assert outcomes[0].success
        router.round_channel.send.assert_awaited_once()

    async def test_publish_status_event_failed_at_round_1_has_no_channel(self) -> None:
        from unittest.mock import AsyncMock

        case = AuctionCase(
            court_name="c",
            case_number="n",
            first_seen_at=utc(2026, 7, 1),
            updated_at=utc(2026, 7, 1),
            rounds=[AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10)],
        )
        event = apply_status_transition(case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 1))
        router = AuctionNotificationRouter(
            new_channel=AsyncMock(),
            upcoming_channel=AsyncMock(),
            round_channel=AsyncMock(),
            high_score_channel=AsyncMock(),
            suspended_channel=AsyncMock(),
        )
        kinds = await router.publish_status_event(case, event)
        assert kinds == []
