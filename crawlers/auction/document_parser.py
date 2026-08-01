"""Turns a parsed announcement's document links into repository-ready
records, and detects an unchanged re-fetch via content hash.

Kept separate from parser.py: parsing the announcement page itself and
deciding what to do with its attachments/dedup status are independent
concerns. The dedup check exists so the ingestion pipeline
(apps/services/auction_pipeline.py) doesn't reprocess or re-notify for a
byte-identical page re-fetch -- crawler etiquette (spec section 十六:
minimize redundant load on the source).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from crawlers.auction.parser import ParsedAnnouncement

MAX_DOCUMENT_TITLE_LENGTH = 255


@dataclass(frozen=True)
class DocumentRecord:
    doc_type: str
    url: str
    title: str
    fetched_at: datetime
    content_hash: str


def extract_documents(parsed: ParsedAnnouncement, *, fetched_at: datetime | None = None) -> list[DocumentRecord]:
    """Every document extracted here is stamped with the *announcement*
    page's content hash (not a hash of the document's own bytes) -- see
    ``is_unchanged`` and ``database.models.auction.AuctionDocument``'s
    docstring for why.
    """
    stamp = fetched_at or datetime.now(timezone.utc)
    return [
        DocumentRecord(
            doc_type=link.doc_type,
            url=link.url,
            title=link.title[:MAX_DOCUMENT_TITLE_LENGTH],
            fetched_at=stamp,
            content_hash=parsed.source_hash,
        )
        for link in parsed.document_links
    ]


def is_unchanged(previous_hashes: set[str], parsed: ParsedAnnouncement) -> bool:
    """True if ``parsed`` is a byte-identical re-fetch of an announcement already ingested for this case.

    ``previous_hashes`` should be every ``content_hash`` already recorded
    for the case (e.g. ``{doc.content_hash for doc in case.documents if
    doc.content_hash}``) -- a case accumulates several *different*
    announcements over its lifetime, so checking only the most-recently-
    seen hash would miss re-fetches of an older (but already-ingested)
    announcement.
    """
    return parsed.source_hash in previous_hashes
