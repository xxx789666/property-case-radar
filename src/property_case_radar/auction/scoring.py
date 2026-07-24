"""Surface discount rate, risk assessment, and investment scoring.

Implements taiwan_real_estate_radar.md section 八. Two distinct numbers
come out of this module, matching the Discord example in section 六:

    表面折價率 (surface_discount_rate)     -- a single ratio
    案件投資分數 (investment_score, /100)   -- the "建議總分配置" breakdown
    風險評分 (risk_level, categorical)      -- derived from the delivery +
                                               ownership sub-scores only

IMPORTANT (spec section 十六): the surface discount rate explicitly
excludes taxes, arrears, renovation, eviction, and litigation costs. Any
caller-facing text derived from this module should keep saying so --
see notifications.py's use of SURFACE_DISCOUNT_DISCLAIMER.
"""

from __future__ import annotations

from dataclasses import dataclass

from property_case_radar.auction.models import AuctionCase, OccupancyStatus, OwnershipType

SURFACE_DISCOUNT_DISCLAIMER = "表面折價率不等於實際獲利，應另計稅費、欠費、裝修、搬遷與訴訟成本，投標前請重新確認公告、點交與產權狀態。"


def surface_discount_rate(floor_unit_price: float, regional_avg_unit_price: float) -> float:
    """1 - 法拍底價單價 ÷ 區域成交單價 (spec section 八).

    Raises ValueError if the regional average is not a usable positive
    number -- there is no meaningful discount rate without one.
    """
    if regional_avg_unit_price <= 0:
        raise ValueError("regional_avg_unit_price must be positive")
    if floor_unit_price < 0:
        raise ValueError("floor_unit_price must not be negative")
    return 1 - (floor_unit_price / regional_avg_unit_price)


# --- 點交狀態 (20 分) -------------------------------------------------

_DELIVERY_POINTS: dict[OccupancyStatus, float] = {
    OccupancyStatus.VACANT_DELIVERABLE: 20.0,  # 點交、無占用：加分
    OccupancyStatus.OCCUPIED_DELIVERABLE: 14.0,  # 點交但有人居住：小幅扣分
    OccupancyStatus.LEASE_EXISTS: 10.0,  # 有租賃關係：需檢查租約
    OccupancyStatus.NOT_DELIVERABLE: 4.0,  # 不點交：大幅扣分
    OccupancyStatus.THIRD_PARTY_OCCUPIED: 0.0,  # 第三人占用：高風險
    OccupancyStatus.UNKNOWN: 8.0,  # unknown: conservative mid-low score
}


def delivery_score(case: AuctionCase) -> float:
    return _DELIVERY_POINTS[case.occupancy_status]


# --- 產權完整 (20 分) -------------------------------------------------

_OWNERSHIP_POINTS: dict[OwnershipType, float] = {
    OwnershipType.FULL: 20.0,  # 完整所有權：加分
    OwnershipType.PARTIAL_SHARE: 6.0,  # 持分拍賣：大幅扣分
    OwnershipType.LAND_ONLY: 10.0,  # 只有土地或只有建物：扣分
    OwnershipType.BUILDING_ONLY: 10.0,
    # Unknown ownership must score conservatively below FULL -- treating
    # "we don't know" the same as "confirmed clean title" would hide risk
    # from a case whose 產權 field simply failed to parse. 8/20 mirrors the
    # same conservative ratio used for OccupancyStatus.UNKNOWN in
    # _DELIVERY_POINTS above.
    OwnershipType.UNKNOWN: 8.0,
}


def ownership_score(case: AuctionCase) -> float:
    score = _OWNERSHIP_POINTS[case.ownership_type]
    if case.has_unregistered_addition:  # 增建未登記：扣分
        score = max(0.0, score - 4.0)
    return score


# --- 拍次與時程 (10 分) -----------------------------------------------


def round_timing_score(case: AuctionCase) -> float:
    """第一拍折價通常較低；第二拍具吸引力；第三拍以後折價更高但風險也升高.

    This component scores price-progression attractiveness, not risk --
    round-related risk is already reflected qualitatively via round
    number appearing in /auction risk output.
    """
    round_number = case.round_number
    if round_number is None:
        return 0.0
    if round_number <= 1:
        return 5.0
    if round_number == 2:
        return 8.0
    if round_number == 3:
        return 10.0
    return 6.0  # 應買階段: attractive price, but needs separate judgement


# --- 價格折價 (35 分) --------------------------------------------------

# A 50% surface discount rate maps to full marks; 0% or negative maps to 0.
_PRICE_DISCOUNT_FULL_MARK_RATE = 0.5
_PRICE_DISCOUNT_MAX_POINTS = 35.0


def price_discount_score(discount_rate: float) -> float:
    if discount_rate <= 0:
        return 0.0
    ratio = min(1.0, discount_rate / _PRICE_DISCOUNT_FULL_MARK_RATE)
    return ratio * _PRICE_DISCOUNT_MAX_POINTS


# --- 區域流動性 (10 分) -----------------------------------------------


def liquidity_score(liquidity_index: float) -> float:
    """``liquidity_index`` in [0, 1]; 0.5 (neutral/unknown) -> 5 points."""
    clamped = min(1.0, max(0.0, liquidity_index))
    return clamped * 10.0


# --- 資料完整程度 (5 分) -----------------------------------------------

_COMPLETENESS_FIELDS = (
    "building_area_ping",
    "land_area_ping",
    "ownership_ratio",
    "announcement_url",
    "announced_date",
)


def completeness_score(case: AuctionCase) -> float:
    populated = sum(1 for name in _COMPLETENESS_FIELDS if getattr(case, name))
    has_docs = bool(case.documents)
    has_occupancy = case.occupancy_status != OccupancyStatus.UNKNOWN
    total_checks = len(_COMPLETENESS_FIELDS) + 2
    populated += int(has_docs) + int(has_occupancy)
    return (populated / total_checks) * 5.0


@dataclass(frozen=True)
class ScoreBreakdown:
    surface_discount_rate: float
    price_discount: float  # /35
    delivery: float  # /20
    ownership: float  # /20
    round_timing: float  # /10
    liquidity: float  # /10
    completeness: float  # /5

    @property
    def investment_score(self) -> float:
        total = (
            self.price_discount
            + self.delivery
            + self.ownership
            + self.round_timing
            + self.liquidity
            + self.completeness
        )
        return round(min(100.0, total), 1)


def score_case(case: AuctionCase, regional_avg_unit_price: float, liquidity_index: float = 0.5) -> ScoreBreakdown:
    """Compute the full investment-score breakdown (spec section 八 "建議總分配置")."""
    current_round = case.current_round
    if current_round is None:
        raise ValueError("case has no rounds; cannot score without a floor price")
    rate = surface_discount_rate(current_round.floor_unit_price, regional_avg_unit_price)
    return ScoreBreakdown(
        surface_discount_rate=rate,
        price_discount=price_discount_score(rate),
        delivery=delivery_score(case),
        ownership=ownership_score(case),
        round_timing=round_timing_score(case),
        liquidity=liquidity_score(liquidity_index),
        completeness=completeness_score(case),
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


def risk_score_0_100(case: AuctionCase) -> float:
    """0-100 risk score, higher = safer. Used by /auction risk detail output."""
    combined = delivery_score(case) + ownership_score(case)
    return round((combined / 40.0) * 100.0, 1)
