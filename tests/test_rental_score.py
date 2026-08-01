from scoring.rental_score import RentalScoreInput, score_rental


def test_rental_score_rewards_below_market_affordable_complete_listing() -> None:
    result = score_rental(
        RentalScoreInput(
            monthly_rent_twd=15_000,
            rent_per_ping_twd=750,
            district_median_rent_per_ping_twd=1_000,
            completeness_ratio=1,
            feature_count=5,
            price_drop_rate=0.1,
        )
    )
    assert result.total == 100
    assert float(result.discount_rate) == 0.25


def test_rental_score_is_bounded() -> None:
    result = score_rental(
        RentalScoreInput(
            monthly_rent_twd=100_000,
            rent_per_ping_twd=2_000,
            district_median_rent_per_ping_twd=1_000,
            completeness_ratio=0,
            feature_count=0,
        )
    )
    assert result.total == 0
