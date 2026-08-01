from tools.radar_agent_subscription import _parser, _payload


def test_rental_subscription_payload() -> None:
    args = _parser().parse_args(
        [
            "rental-subscribe",
            "--city",
            "桃園市",
            "--district",
            "中壢區",
            "--max-monthly-rent-twd",
            "30000",
            "--rental-type",
            "entire_home",
            "--layout-contains",
            "2房",
            "--features-contains",
            "有電梯",
            "--keyword",
            "住辦",
            "--keyword",
            "店面",
            "--min-score",
            "80",
        ]
    )
    assert _payload(args) == {
        "operation": "rental-create",
        "city": "桃園市",
        "district": "中壢區",
        "max_monthly_rent_twd": 30_000,
        "rental_type": "entire_home",
        "layout_contains": "2房",
        "features_contains": "有電梯",
        "keywords_any": ["住辦", "店面"],
        "min_score": 80,
    }


def test_cancel_accepts_rental_kind() -> None:
    args = _parser().parse_args(["cancel", "--kind", "rental", "--id", "7"])
    assert _payload(args) == {"operation": "cancel", "kind": "rental", "id": 7}
