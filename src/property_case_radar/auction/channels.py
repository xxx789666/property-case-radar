"""Discord channel configuration for the auction vertical.

Values copied verbatim from "discord 伺服器.txt" (主伺服器頻道
1530072733818556538). Per CLAUDE.md: "Treat these IDs as
environment-specific configuration, not something to invent or change."
Only the 法拍案件-* channels and the private 法拍案件-搜尋 channel are
relevant here; the 房地案件-* (sale pipeline) channels are out of scope
for this vertical.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuctionChannel:
    name: str
    channel_id: str
    is_public: bool  # public channels are 禁言 (announcement-only); no PII allowed


GUILD_ID = "1530072733818556538"

NEW_ANNOUNCEMENT = AuctionChannel("法拍案件-新公告", "1530075570140876982", is_public=True)
UPCOMING_AUCTION = AuctionChannel("法拍案件-即將開標", "1530075622636781639", is_public=True)
SECOND_THIRD_ROUND = AuctionChannel("法拍案件-二拍三拍", "1530075673052577922", is_public=True)
HIGH_SCORE = AuctionChannel("法拍案件-高分案件", "1530075701150089409", is_public=True)
SUSPENDED_WITHDRAWN = AuctionChannel("法拍案件-停拍撤回", "1530075739750268989", is_public=True)
SEARCH_PRIVATE = AuctionChannel("法拍案件-搜尋", "1530076529751756870", is_public=False)

ALL_PUBLIC_CHANNELS = (
    NEW_ANNOUNCEMENT,
    UPCOMING_AUCTION,
    SECOND_THIRD_ROUND,
    HIGH_SCORE,
    SUSPENDED_WITHDRAWN,
)

HIGH_SCORE_THRESHOLD = 80.0
UPCOMING_WITHIN_DAYS = 7
