from decimal import Decimal

from scoring.land_score import LandScoreInput, score_land


def test_land_score_uses_objective_100_point_composition() -> None:
    score = score_land(
        LandScoreInput(
            listing_unit_price=700_000,
            market_unit_price=1_000_000,
            transaction_count=20,
            completeness_ratio=1,
            price_drop_rate=0.10,
        )
    )
    assert score.discount_rate == Decimal("0.3000")
    assert score.price_discount == 50
    assert score.market_liquidity == 25
    assert score.price_change == 10
    assert score.data_completeness == 15
    assert score.total == 100


def test_land_score_is_bounded_for_overpriced_sparse_listing() -> None:
    score = score_land(
        LandScoreInput(
            listing_unit_price=1_500_000,
            market_unit_price=1_000_000,
            transaction_count=2,
            completeness_ratio=0.5,
        )
    )
    assert score.discount_rate == Decimal("-0.5000")
    assert score.price_discount == 0
    assert 0 <= score.total <= 100
