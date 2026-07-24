from datetime import date

import pytest

from crawlers.transaction.actual_price import ActualTransactionRecord, summarize_market


def test_shared_market_price_summary() -> None:
    summary = summarize_market(
        [
            ActualTransactionRecord(
                city="桃園市",
                district="中壢區",
                transaction_date=date(2026, 1, 2),
                unit_price_per_ping_twd=390_000,
                total_price_twd=12_000_000,
                building_area_ping=30,
                building_type="住宅",
            ),
            ActualTransactionRecord(
                city="桃園市",
                district="中壢區",
                transaction_date=date(2026, 2, 2),
                unit_price_per_ping_twd=398_000,
                total_price_twd=14_000_000,
                building_area_ping=35,
                building_type="住宅",
            ),
        ]
    )
    assert summary.average_unit_price_twd == 394_000
    assert summary.transaction_count == 2
    assert summary.period_start == date(2026, 1, 2)


def test_market_summary_rejects_mixed_areas() -> None:
    records = [
        ActualTransactionRecord("桃園市", "中壢區", date(2026, 1, 1), 1, 1, 1),
        ActualTransactionRecord("桃園市", "桃園區", date(2026, 1, 1), 1, 1, 1),
    ]
    with pytest.raises(ValueError, match="same city"):
        summarize_market(records)
