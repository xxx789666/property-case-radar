"""Surface discount rate, risk assessment, and investment scoring for auctions.

Implements taiwan_real_estate_radar.md section 八. Three distinct numbers
come out of this module, matching the Discord example in section 六:

    表面折價率 (calculate_surface_discount_rate) -- a single ratio
    案件投資分數 (score_auction_case, /100)        -- the "建議總分配置" breakdown
    風險評分 (risk_level, categorical)             -- derived from the
                                                      delivery + ownership
                                                      sub-scores only

All prices are whole TWD, mirroring ``scoring.sale_score`` and
``database.models.common.MarketPrice.average_unit_price_twd`` -- there is
only one unit convention shared by both pipelines.

IMPORTANT (spec section 十六): the surface discount rate explicitly
excludes taxes, arrears, renovation, eviction, and litigation costs. Any
caller-facing text derived from this module should keep saying so -- see
notifications/auction_notification.py's use of SURFACE_DISCOUNT_DISCLAIMER.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from database.models.auction import AuctionCase, OccupancyStatus, OwnershipType

SURFACE_DISCOUNT_DISCLAIMER = (
    "表面折價率不等於實際獲利，應另計稅費、欠費、裝修、搬遷與訴訟成本，投標前請重新確認公告、點交與產權狀態。"
)


def calculate_surface_discount_rate(floor_unit_price_twd: int, market_unit_price_twd: int) -> Decimal:
    """1 - 法拍底價單價 ÷ 區域成交單價 (spec section 八).

    Mirrors ``scoring.sale_score.calculate_discount_rate``'s Decimal
    convention so the two pipelines format/round identically.
    """
    if market_unit_price_twd <= 0:
        raise ValueError("market_unit_price_twd must be positive")
    if floor_unit_price_twd < 0:
        raise ValueError("floor_unit_price_twd must not be negative")
    return (Decimal(1) - Decimal(floor_unit_price_twd) / Decimal(market_unit_price_twd)).quantize(Decimal("0.0001"))


# --- 點交狀態 (20 分) -------------------------------------------------

_DELIVERY_POINTS: dict[OccupancyStatus, int] = {
    OccupancyStatus.VACANT_DELIVERABLE: 20,  # 點交、無占用：加分
    OccupancyStatus.OCCUPIED_DELIVERABLE: 14,  # 點交但有人居住：小幅扣分
    OccupancyStatus.LEASE_EXISTS: 10,  # 有租賃關係：需檢查租約
    OccupancyStatus.NOT_DELIVERABLE: 4,  # 不點交：大幅扣分
    OccupancyStatus.THIRD_PARTY_OCCUPIED: 0,  # 第三人占用：高風險
    OccupancyStatus.UNKNOWN: 8,  # unknown: conservative mid-low score
}


def delivery_score(case: AuctionCase) -> int:
    return _DELIVERY_POINTS[case.occupancy_status]


# --- 產權完整 (20 分) -------------------------------------------------

_OWNERSHIP_POINTS: dict[OwnershipType, int] = {
    OwnershipType.FULL: 20,  # 完整所有權：加分
    OwnershipType.PARTIAL_SHARE: 6,  # 持分拍賣：大幅扣分
    OwnershipType.LAND_ONLY: 10,  # 只有土地或只有建物：扣分
    OwnershipType.BUILDING_ONLY: 10,
    # Unknown ownership must score conservatively below FULL -- treating
    # "we don't know" the same as "confirmed clean title" would hide risk
    # from a case whose 產權 field simply failed to parse.
    OwnershipType.UNKNOWN: 8,
}


def ownership_score(case: AuctionCase) -> int:
    score = _OWNERSHIP_POINTS[case.ownership_type]
    if case.has_unregistered_addition:  # 增建未登記：扣分
        score = max(0, score - 4)
    return score


# --- 拍次與時程 (10 分) -----------------------------------------------


def round_timing_score(case: AuctionCase) -> int:
    """第一拍折價通常較低；第二拍具吸引力；第三拍以後折價更高但風險也升高."""
    round_number = case.round_number
    if round_number is None:
        return 0
    if round_number <= 1:
        return 5
    if round_number == 2:
        return 8
    if round_number == 3:
        return 10
    return 6  # 應買階段: attractive price, but needs separate judgement


# --- 價格折價 (35 分) --------------------------------------------------

_PRICE_DISCOUNT_FULL_MARK_RATE = Decimal("0.5")
_PRICE_DISCOUNT_MAX_POINTS = 35


def price_discount_score(discount_rate: Decimal) -> int:
    if discount_rate <= 0:
        return 0
    ratio = min(Decimal(1), discount_rate / _PRICE_DISCOUNT_FULL_MARK_RATE)
    return round(ratio * _PRICE_DISCOUNT_MAX_POINTS)


# --- 區域流動性 (10 分) -----------------------------------------------


def liquidity_score(liquidity_index: float) -> int:
    """``liquidity_index`` in [0, 1]; 0.5 (neutral/unknown) -> 5 points."""
    clamped = min(1.0, max(0.0, liquidity_index))
    return round(clamped * 10)


# --- 資料完整程度 (5 分) -----------------------------------------------

_COMPLETENESS_FIELDS = ("building_area_ping", "land_area_ping", "ownership_ratio", "announcement_url", "announced_date")


def completeness_score(case: AuctionCase) -> int:
    populated = sum(1 for name in _COMPLETENESS_FIELDS if getattr(case, name))
    has_docs = bool(case.documents)
    has_occupancy = case.occupancy_status != OccupancyStatus.UNKNOWN
    total_checks = len(_COMPLETENESS_FIELDS) + 2
    populated += int(has_docs) + int(has_occupancy)
    return round((populated / total_checks) * 5)


@dataclass(frozen=True)
class AuctionScore:
    total: int
    surface_discount_rate: Decimal
    price_discount: int  # /35
    delivery: int  # /20
    ownership: int  # /20
    round_timing: int  # /10
    liquidity: int  # /10
    completeness: int  # /5


def score_auction_case(case: AuctionCase, regional_avg_unit_price_twd: int, liquidity_index: float = 0.5) -> AuctionScore:
    """Compute the full investment-score breakdown (spec section 八 "建議總分配置")."""
    current_round = case.current_round
    if current_round is None:
        raise ValueError("case has no rounds; cannot score without a floor price")
    rate = calculate_surface_discount_rate(current_round.floor_unit_price_twd, regional_avg_unit_price_twd)
    price_discount = price_discount_score(rate)
    delivery = delivery_score(case)
    ownership = ownership_score(case)
    timing = round_timing_score(case)
    liquidity = liquidity_score(liquidity_index)
    completeness = completeness_score(case)
    total = price_discount + delivery + ownership + timing + liquidity + completeness
    return AuctionScore(
        total=max(0, min(100, total)),
        surface_discount_rate=rate,
        price_discount=price_discount,
        delivery=delivery,
        ownership=ownership,
        round_timing=timing,
        liquidity=liquidity,
        completeness=completeness,
    )


# --- 風險評分 (categorical, e.g. "中低風險") ---------------------------
#
# Derived only from the delivery + ownership sub-scores (each /20, so the
# combined max is 40) -- these are the two risk-specific components
# called out in section 八 ("點交與占用風險" / "產權與法律風險"), as
# opposed to price/liquidity which are opportunity, not risk.

RISK_LEVELS = ("低風險", "中低風險", "中風險", "中高風險", "高風險")


def risk_level(case: AuctionCase) -> str:
    combined = delivery_score(case) + ownership_score(case)  # out of 40
    ratio = combined / 40.0
    if ratio >= 0.85:
        return "低風險"
    if ratio >= 0.65:
        return "中低風險"
    if ratio >= 0.45:
        return "中風險"
    if ratio >= 0.25:
        return "中高風險"
    return "高風險"


def risk_score_0_100(case: AuctionCase) -> int:
    """0-100 risk score, higher = safer. Used by /auction risk detail output."""
    combined = delivery_score(case) + ownership_score(case)
    return round((combined / 40.0) * 100)
