"""Natural-person name masking for public Discord notifications.

taiwan_real_estate_radar.md section 七: "涉及自然人姓名等資料時，Discord
公開頻道建議只顯示案件必要資訊，避免大量推播個資" (when natural-person
data such as names is involved, public Discord channels should show only
the necessary case info, avoiding broadcasting PII at scale).

The natural-person fields on AuctionCase are ``debtor`` (債務人) and
``owner`` (所有權人). This module masks those names and provides a
helper to build a "public-safe" copy of a case for use by
notifications.py and the public branches of discord/commands.py. Full,
unmasked case data remains available for the private search channels
(法拍案件-搜尋 per discord 伺服器.txt) and for a user's own
``/auction detail`` lookups where showing PII is expected and scoped to
one requester.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Literal

from property_case_radar.auction.models import AuctionCase

Audience = Literal["public", "private"]

_MASK_CHAR = "○"
_SEPARATOR_RE = re.compile(r"[、,，/\s]+")


def mask_name(name: str) -> str:
    """Mask a single Chinese/generic personal name, keeping only the first character.

    王小明 -> 王○○   王明 -> 王○   A -> A (single char, nothing to mask)
    Empty/whitespace input returns "" unchanged.
    """
    name = name.strip()
    if len(name) <= 1:
        return name
    return name[0] + _MASK_CHAR * (len(name) - 1)


def mask_name_field(field_value: str) -> str:
    """Mask a field that may contain multiple names separated by 、 , / or whitespace.

    e.g. "王小明、陳大文" -> "王○○、陳○○". Preserves the original
    separators found in the string (uses "、" if none were present).
    """
    field_value = field_value.strip()
    if not field_value:
        return field_value
    parts = [p for p in _SEPARATOR_RE.split(field_value) if p]
    return "、".join(mask_name(p) for p in parts)


def redact_case_for_public(case: AuctionCase) -> AuctionCase:
    """Return a shallow copy of ``case`` with debtor/owner names masked.

    Use this for any notification or command response destined for a
    public channel (per discord 伺服器.txt: 法拍案件-新公告／即將開標／
    二拍三拍／高分案件／停拍撤回). Private search channels and
    single-user detail lookups should use the original case instead.
    """
    return dataclasses.replace(
        case,
        debtor=mask_name_field(case.debtor),
        owner=mask_name_field(case.owner),
    )


def view_for_audience(case: AuctionCase, audience: Audience) -> AuctionCase:
    """Single choke point used by notifications.py and discord/commands.py.

    Every place that renders a case as user-facing text should route
    through this function rather than deciding ad hoc whether to mask,
    so "which audiences get masked" has exactly one answer.
    """
    return redact_case_for_public(case) if audience == "public" else case
