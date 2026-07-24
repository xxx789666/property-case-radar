from datetime import date, datetime, timezone

from property_case_radar.auction import channels
from property_case_radar.auction.models import AuctionCase, AuctionRound, AuctionStatus
from property_case_radar.auction.notifications import (
    build_case_detail_text,
    build_new_case_notification,
    build_status_update_notification,
    channels_for_new_case,
    channels_for_status_event,
)
from property_case_radar.auction.scoring import score_case
from property_case_radar.auction.state_machine import apply_transition
from property_case_radar.shared.market_prices import InMemoryMarketPriceProvider


def test_build_new_case_notification_format(sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    result = build_new_case_notification(sample_case, market, score, audience="public")
    assert result.text.startswith("⚖️ 新增法拍案件")
    assert "法院：桃園地方法院" in result.text
    assert "案號：115年度司執字第XXXX號" in result.text
    assert "底價：980 萬" in result.text
    assert "表面折價率：35.2%" in result.text
    assert "點交：是" in result.text
    assert "拍賣日期：2026-08-18" in result.text
    # never leaks debtor/owner, masked or not, in the push notification
    assert "王" not in result.text


def test_build_new_case_notification_public_includes_disclaimer(
    sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider
) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    public = build_new_case_notification(sample_case, market, score, audience="public")
    private = build_new_case_notification(sample_case, market, score, audience="private")
    assert "表面折價率不等於實際獲利" in public.text
    assert "表面折價率不等於實際獲利" not in private.text


def test_build_case_detail_text_masks_by_audience(
    sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider
) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    public = build_case_detail_text(sample_case, market, score, audience="public")
    private = build_case_detail_text(sample_case, market, score, audience="private")
    assert "債務人：王○○" in public.text
    assert "債務人：王小明" in private.text
    assert "所有權人：王○○" in public.text
    assert "所有權人：王小明" in private.text


def test_build_status_update_notification_format(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.SUSPENDED,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    result = build_status_update_notification(sample_case, event, audience="public")
    assert result.text.startswith("⚠️ 法拍案件狀態更新")
    assert "原狀態：新增公告" in result.text
    assert "新狀態：停拍" in result.text
    assert "原定日期：2026-08-18" in result.text
    assert "請勿再依原日期前往投標" in result.text


def test_channels_for_new_case_routes_second_round_to_second_third_channel(
    sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider
) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    targets = channels_for_new_case(sample_case, score)
    assert channels.NEW_ANNOUNCEMENT in targets
    assert channels.SECOND_THIRD_ROUND in targets  # round_number == 2


def test_channels_for_new_case_routes_high_score(sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    targets = channels_for_new_case(sample_case, score)
    if score.investment_score >= channels.HIGH_SCORE_THRESHOLD:
        assert channels.HIGH_SCORE in targets
    else:
        assert channels.HIGH_SCORE not in targets


def test_channels_for_status_event_suspended_goes_only_to_suspended_channel(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.WITHDRAWN,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    targets = channels_for_status_event(sample_case, event)
    assert targets == [channels.SUSPENDED_WITHDRAWN]


def test_status_update_note_hidden_for_public_shown_for_private(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.CORRECTED,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        note="債務人王小明對本次公告提出異議",
    )
    public = build_status_update_notification(sample_case, event, audience="public")
    private = build_status_update_notification(sample_case, event, audience="private")
    assert "王小明" not in public.text
    assert "備註" not in public.text
    assert "王小明" in private.text
    assert "備註：債務人王小明對本次公告提出異議" in private.text


def test_status_update_renders_price_change_payload(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.PRICE_CHANGED,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        new_floor_price_total=784.0,
        new_floor_unit_price=18.5,
    )
    result = build_status_update_notification(sample_case, event, audience="public")
    assert "底價：980 萬 → 784 萬" in result.text
    assert "底價單價：23.2 萬／坪 → 18.5 萬／坪" in result.text


def test_status_update_renders_date_change_payload(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.DATE_CHANGED,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        new_auction_date=date(2026, 9, 29),
    )
    result = build_status_update_notification(sample_case, event, audience="public")
    assert "拍賣日期：2026-08-18 → 2026-09-29" in result.text


def test_channels_for_status_event_failed_routes_to_second_third_only_at_round_2_plus(
    sample_case: AuctionCase,
) -> None:
    # sample_case starts at round 2
    event = apply_transition(sample_case, AuctionStatus.FAILED, changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert channels_for_status_event(sample_case, event) == [channels.SECOND_THIRD_ROUND]


def test_channels_for_status_event_failed_at_round_1_has_no_channel() -> None:
    case = AuctionCase(
        case_id="round1",
        court_name="法院",
        case_number="案號",
        first_seen_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        rounds=[AuctionRound(round_number=1, floor_price_total=1200.0, floor_unit_price=28.4)],
    )
    event = apply_transition(case, AuctionStatus.FAILED, changed_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert channels_for_status_event(case, event) == []


def test_channels_for_status_event_awarded_has_no_channel(sample_case: AuctionCase) -> None:
    event = apply_transition(
        sample_case,
        AuctionStatus.AWARDED,
        changed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        winning_price=820.0,
    )
    assert channels_for_status_event(sample_case, event) == []


def test_channels_for_new_case_excludes_terminal_and_failed_status(
    sample_case: AuctionCase, market_prices: InMemoryMarketPriceProvider
) -> None:
    market = market_prices.get_regional_average("桃園市", "中壢區")
    score = score_case(sample_case, market.avg_unit_price_per_ping)
    for status in (AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED, AuctionStatus.FAILED):
        sample_case.status = status  # direct assignment: only routing behavior under test here
        assert channels_for_new_case(sample_case, score) == []
