from datetime import date

from apps.services.rental_daily_summary import build_daily_rental_summary_embed


def test_rental_summary_distinguishes_failure_from_zero() -> None:
    embed = build_daily_rental_summary_embed(
        date(2026, 8, 4),
        {"桃園市": 0, "新竹縣": 0},
        failed_regions=("桃園市",),
    )

    assert "桃園市：抓取失敗／待重試" in (embed.description or "")
    assert "新竹縣：0 筆" in (embed.description or "")
    assert "待重試：桃園市" in (embed.footer.text or "")
