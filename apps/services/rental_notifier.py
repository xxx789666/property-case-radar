"""Short-lived Discord REST notifier for periodic rental jobs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import discord

from apps.config import Settings
from apps.services.auction_notifier import DiscordRestChannelSender
from notifications.rental_notification import RentalNotificationRouter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def live_rental_notifier(
    settings: Settings,
) -> AsyncIterator[RentalNotificationRouter | None]:
    if not settings.discord_token:
        logger.warning("DISCORD_TOKEN is not set; rental notifications are disabled")
        yield None
        return
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(settings.discord_token)
    except discord.LoginFailure:
        logger.error("Discord login failed; rental notifications are disabled")
        await client.close()
        yield None
        return
    try:
        yield RentalNotificationRouter(
            new_channel=DiscordRestChannelSender(
                client, settings.discord_rental_new_channel_id
            ),
            price_drop_channel=DiscordRestChannelSender(
                client, settings.discord_rental_price_drop_channel_id
            ),
            high_score_channel=DiscordRestChannelSender(
                client, settings.discord_rental_high_score_channel_id
            ),
            high_score_threshold=settings.rental_high_score_threshold,
        )
    finally:
        await client.close()
