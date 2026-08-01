from decimal import Decimal

import pytest

from scoring.sale_score import SaleScoreInput, calculate_discount_rate, score_sale


def test_discount_formula_matches_spec() -> None:
    assert calculate_discount_rate(351_000, 394_000) == Decimal("0.1091")


def test_score_is_component_sum_and_capped() -> None:
    result = score_sale(
        SaleScoreInput(
            listing_unit_price=280_000,
            market_unit_price=400_000,
            transaction_count=35,
            age_years=10,
            has_parking=True,
            completeness_ratio=1,
            price_drop_rate=0.1,
        )
    )
    assert result.total == 96
    assert result.price_discount == 40


@pytest.mark.parametrize("listing,market", [(0, 1), (1, 0), (-1, 2)])
def test_discount_rejects_invalid_prices(listing: int, market: int) -> None:
    with pytest.raises(ValueError):
        calculate_discount_rate(listing, market)
