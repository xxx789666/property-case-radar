from datetime import date, datetime, timezone

import pytest

from property_case_radar.auction.discord.commands import AuctionCommandHandlers, CommandResponse
from property_case_radar.auction.models import AuctionCase, AuctionFilter, AuctionStatus, CaseType
from property_case_radar.auction.repository import InMemoryAuctionRepository
from property_case_radar.auction.state_machine import apply_transition
from property_case_radar.shared.market_prices import InMemoryMarketPriceProvider


@pytest.fixture
def repo(sample_case: AuctionCase) -> InMemoryAuctionRepository:
    sample_case.investment_score = 88.0
    repo = InMemoryAuctionRepository()
    repo.add_case(sample_case)
    return repo


@pytest.fixture
def handlers(repo: InMemoryAuctionRepository, market_prices: InMemoryMarketPriceProvider) -> AuctionCommandHandlers:
    return AuctionCommandHandlers(repo, market_prices)


class TestSearch:
    def test_search_matches(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.search(AuctionFilter(city="桃園市"))
        assert "符合條件的法拍案件" in response.text
        assert "115年度司執字第XXXX號" in response.text

    def test_search_no_matches(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.search(AuctionFilter(city="台北市"))
        assert "查無符合條件" in response.text

    def test_search_public_audience_masks_nothing_extra_since_summary_has_no_pii(
        self, handlers: AuctionCommandHandlers
    ) -> None:
        # summary lines never include debtor/owner, so public vs private
        # summaries should be identical in content (masking is exercised
        # by detail()/risk() instead).
        public = handlers.search(AuctionFilter(), audience="public")
        private = handlers.search(AuctionFilter(), audience="private")
        assert public.text == private.text
        assert "王" not in public.text


class TestSubscribeUnsubscribe:
    def test_subscribe_then_unsubscribe(self, handlers: AuctionCommandHandlers, repo: InMemoryAuctionRepository) -> None:
        sub_response = handlers.subscribe("user-1", AuctionFilter(city="桃園市", min_round=2))
        assert "已建立法拍案件訂閱" in sub_response.text
        subs = repo.list_subscriptions("user-1")
        assert len(subs) == 1
        sub_id = subs[0].subscription_id

        unsub_response = handlers.unsubscribe("user-1", sub_id)
        assert "已取消訂閱" in unsub_response.text
        assert repo.list_subscriptions("user-1") == []

    def test_unsubscribe_wrong_user_fails(self, handlers: AuctionCommandHandlers) -> None:
        sub_response_text = handlers.subscribe("user-1", AuctionFilter()).text
        assert sub_response_text  # created
        response = handlers.unsubscribe("someone-else", "not-a-real-id")
        assert "找不到訂閱編號" in response.text

    def test_unsubscribe_cannot_target_another_users_subscription(self, handlers: AuctionCommandHandlers, repo: InMemoryAuctionRepository) -> None:
        handlers.subscribe("user-1", AuctionFilter(), subscription_id="sub-1")
        response = handlers.unsubscribe("user-2", "sub-1")
        assert "找不到訂閱編號" in response.text
        assert repo.get_subscription("sub-1").active is True


class TestLatest:
    def test_latest_returns_case(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.latest()
        assert "最新法拍案件" in response.text
        assert "115年度司執字第XXXX號" in response.text

    def test_latest_empty_repository(self, market_prices: InMemoryMarketPriceProvider) -> None:
        empty_handlers = AuctionCommandHandlers(InMemoryAuctionRepository(), market_prices)
        response = empty_handlers.latest()
        assert "沒有法拍案件資料" in response.text


class TestDetail:
    def test_detail_private_shows_unmasked_debtor(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.detail("桃園地方法院", "115年度司執字第XXXX號", audience="private")
        assert "債務人：王小明" in response.text

    def test_detail_public_masks_debtor(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.detail("桃園地方法院", "115年度司執字第XXXX號", audience="public")
        assert "債務人：王○○" in response.text
        assert "王小明" not in response.text

    def test_detail_unknown_case(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.detail("不存在法院", "不存在案號")
        assert "查無案件" in response.text

    def test_detail_default_audience_is_public_safe(self, handlers: AuctionCommandHandlers) -> None:
        # If a future adapter forgets to pass audience at all, the
        # omitted-argument default must still mask PII, not leak it.
        response = handlers.detail("桃園地方法院", "115年度司執字第XXXX號")
        assert response.audience == "public"
        assert "王○○" in response.text
        assert "王小明" not in response.text


class TestCommandResponseDefault:
    def test_command_response_default_audience_is_public(self) -> None:
        assert CommandResponse(text="x").audience == "public"


class TestSchedule:
    def test_schedule_within_window(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.schedule(within_days=60, today=date(2026, 7, 24))
        assert "開標案件" in response.text
        assert "2026-08-18" in response.text

    def test_schedule_outside_window(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.schedule(within_days=1, today=date(2026, 7, 24))
        assert "沒有即將開標" in response.text

    def test_schedule_default_audience_is_public(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.schedule(within_days=60, today=date(2026, 7, 24))
        assert response.audience == "public"

    @pytest.mark.parametrize(
        "status",
        [AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED, AuctionStatus.FAILED],
    )
    def test_schedule_excludes_terminal_and_failed_cases(
        self, handlers: AuctionCommandHandlers, repo: InMemoryAuctionRepository, sample_case: AuctionCase, status
    ) -> None:
        # sample_case's auction_date (2026-08-18) is within the window,
        # but once the case is no longer actively scheduled it must not
        # appear in /auction schedule regardless of the stale date on its
        # current round.
        if status == AuctionStatus.AWARDED:
            apply_transition(
                sample_case, status, changed_at=datetime(2026, 7, 25, tzinfo=timezone.utc), winning_price=900.0
            )
        else:
            apply_transition(sample_case, status, changed_at=datetime(2026, 7, 25, tzinfo=timezone.utc))
        response = handlers.schedule(within_days=60, today=date(2026, 7, 24))
        assert "沒有即將開標" in response.text


class TestRisk:
    def test_risk_private_shows_full_detail(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.risk("桃園地方法院", "115年度司執字第XXXX號", audience="private")
        assert "風險評估" in response.text
        assert "風險評分" in response.text
        assert "占用情況：空屋" in response.text

    def test_risk_unknown_case(self, handlers: AuctionCommandHandlers) -> None:
        response = handlers.risk("不存在法院", "不存在案號")
        assert "查無案件" in response.text


def test_search_filters_by_case_type(handlers: AuctionCommandHandlers) -> None:
    response = handlers.search(AuctionFilter(case_type=CaseType.LAND))
    assert "查無符合條件" in response.text
