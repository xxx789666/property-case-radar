"""Natural-person name masking for public Discord auction notifications.

taiwan_real_estate_radar.md section 七: "涉及自然人姓名等資料時，Discord
公開頻道建議只顯示案件必要資訊，避免大量推播個資" (when natural-person
data such as names is involved, public Discord channels should show only
the necessary case info, avoiding broadcasting PII at scale).

The natural-person fields on ``AuctionCase`` are ``debtor`` (債務人) and
``owner`` (所有權人). This module masks those names for rendering.

IMPORTANT: ``AuctionCase`` is a SQLAlchemy ORM row, not a plain dataclass.
Building a "masked copy" by mutating a live, session-attached instance's
``debtor``/``owner`` attributes (the way an earlier dataclass-based
prototype did) would mark the row dirty and risk persisting masked
placeholder text as real data on the next ``session.flush()``/``commit()``.
To avoid that entirely, this module never touches the ORM instance --
``display_debtor_owner`` takes plain strings and returns plain strings,
and callers (notifications/auction_notification.py, the Discord cog) read
``case.debtor``/``case.owner`` themselves and pass them through.
"""

from __future__ import annotations

import re
from typing import Literal

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


def display_debtor_owner(debtor: str, owner: str, *, audience: Audience) -> tuple[str, str]:
    """Single choke point every renderer uses to decide what to show for 債務人/所有權人.

    Use this for any notification or command response; every place that
    renders these two fields should route through this function rather
    than deciding ad hoc whether to mask, so "which audiences get masked"
    has exactly one answer.
    """
    if audience == "public":
        return mask_name_field(debtor), mask_name_field(owner)
    return debtor, owner
