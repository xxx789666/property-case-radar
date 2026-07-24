"""Production Discord notification wiring for the auction scheduler.

Builds a real, working ``AuctionNotificationRouter`` when
``Settings.discord_token`` is configured -- this is deliberately NOT a
persistent gateway-connected bot like ``apps.discord_bot.main.RadarBot``:
running a second, independently-connected bot process against the same
application/token would race on Discord's application-command
``tree.sync()`` (see ``RadarBot``'s docstring) and intermittently wipe
registered slash commands. Instead this logs in via HTTP only
(``discord.Client.login`` without ever calling ``connect``/``start``,
i.e. no gateway session), fetches/sends to channels over plain HTTP
requests, and closes that HTTP session when done. That lifecycle --
open, do a bounded amount of work, close -- is exactly what fits a
periodic scheduler tick; a persistent connection would have to be kept
alive (and reconnected on drops) across ticks for no benefit, since this
process only ever sends a handful of messages every few hours.

The token itself is never logged, printed, exposed in an exception
message, or otherwise surfaced anywhere in this module -- only whether
one is configured and whether login succeeded/failed. A missing or
invalid token disables notifications for the run (logged clearly) rather
than raising -- ``ingest_auction_announcements`` and
``queue_pending_notifications`` already durably queue every notification
regardless of whether a live sender is available this run, so nothing is
lost: it simply waits in the outbox (see
``apps.services.auction_notification_outbox``) until a token is
configured and a future run can deliver it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import discord

from apps.config import Settings
from notifications.auction_notification import AuctionNotificationRouter

logger = logging.getLogger(__name__)


class DiscordRestChannelSender:
    """A ``notifications.auction_notification.MessageChannel`` backed by
    plain HTTP calls (``Client.fetch_channel`` + ``channel.send``), never
    the gateway. The channel object itself is re-fetched on every send
    rather than cached, since this sender's ``Client`` only lives for one
    scheduler tick -- no benefit to caching across a lifetime that short,
    and it avoids ever sending through a stale/closed channel reference.
    """

    def __init__(self, client: discord.Client, channel_id: int) -> None:
        self._client = client
        self._channel_id = channel_id

    async def send(self, *, embed: discord.Embed) -> object:
        channel = await self._client.fetch_channel(self._channel_id)
        return await channel.send(embed=embed)


@asynccontextmanager
async def live_auction_notifier(settings: Settings) -> AsyncIterator[AuctionNotificationRouter | None]:
    """Yield a working, real-Discord-backed ``AuctionNotificationRouter``
    when ``settings.discord_token`` is set and valid; otherwise log
    clearly why and yield ``None``.

    Never raises: a missing or invalid token disables notifications for
    this run rather than crashing the scheduler job that calls this. In
    both the "yields a router" and "yields None" cases, and even if the
    caller's ``async with`` body itself raises, the underlying HTTP
    session is always closed on the way out -- a bounded lifecycle, not
    a connection left dangling across scheduler ticks.
    """
    if not settings.discord_token:
        logger.warning(
            "DISCORD_TOKEN is not set; auction Discord notifications are disabled for this run "
            "(announcements are still ingested and queued in the notification outbox -- see "
            "apps.services.auction_notification_outbox -- and will be delivered once a token is configured "
            "and a future run calls deliver_pending_notifications)"
        )
        yield None
        return

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(settings.discord_token)
    except discord.LoginFailure:
        logger.error(
            "Discord login failed (DISCORD_TOKEN is set but invalid/revoked); "
            "auction Discord notifications are disabled for this run"
        )
        await client.close()
        yield None
        return
    except Exception:
        logger.exception(
            "unexpected error logging into Discord; auction Discord notifications are disabled for this run"
        )
        await client.close()
        yield None
        return

    try:
        yield AuctionNotificationRouter(
            new_channel=DiscordRestChannelSender(client, settings.discord_auction_new_channel_id),
            upcoming_channel=DiscordRestChannelSender(client, settings.discord_auction_upcoming_channel_id),
            round_channel=DiscordRestChannelSender(client, settings.discord_auction_round_channel_id),
            high_score_channel=DiscordRestChannelSender(client, settings.discord_auction_high_score_channel_id),
            suspended_channel=DiscordRestChannelSender(client, settings.discord_auction_suspended_channel_id),
            high_score_threshold=settings.auction_high_score_threshold,
            upcoming_within_days=settings.auction_upcoming_within_days,
        )
    finally:
        await client.close()
