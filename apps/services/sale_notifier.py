"""Short-lived Discord REST notifier for periodic sale-listing jobs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import discord

from apps.config import Settings
from apps.services.auction_notifier import DiscordRestChannelSender
from notifications.sale_notification import SaleNotificationRouter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def live_sale_notifier(
    settings: Settings,
) -> AsyncIterator[SaleNotificationRouter | None]:
    if not settings.discord_token:
        logger.warning("DISCORD_TOKEN is not set; sale Discord notifications are disabled")
        yield None
        return
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(settings.discord_token)
    except discord.LoginFailure:
        logger.error("Discord login failed; sale Discord notifications are disabled")
        await client.close()
        yield None
        return
    try:
        yield SaleNotificationRouter(
            new_channel=DiscordRestChannelSender(
                client, settings.discord_sale_new_channel_id
            ),
            price_drop_channel=DiscordRestChannelSender(
                client, settings.discord_sale_price_drop_channel_id
            ),
            high_score_channel=DiscordRestChannelSender(
                client, settings.discord_sale_high_score_channel_id
            ),
            high_score_threshold=settings.sale_high_score_threshold,
            channel_factory=lambda channel_id: DiscordRestChannelSender(
                client, channel_id
            ),
        )
    finally:
        await client.close()
