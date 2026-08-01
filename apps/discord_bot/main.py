import json
import logging
import os
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from apps.config import get_settings
from apps.discord_bot.auction_cog import AuctionCog
from apps.discord_bot.auction_service import AuctionCommandService
from apps.discord_bot.cog import HouseCog
from apps.discord_bot.service import HouseCommandService
from database.models import Base
from database.session import create_db_engine, create_session_factory

EXPECTED_COMMAND_SCHEMA = {
    "house": frozenset({"search", "subscribe", "latest", "detail", "compare", "unsubscribe"}),
    "auction": frozenset({"search", "subscribe", "latest", "detail", "schedule", "risk", "unsubscribe"}),
}


def validate_command_schema(commands_to_check: Iterable[app_commands.Command | app_commands.Group]) -> dict[str, int]:
    actual = {
        command.name: frozenset(child.name for child in command.commands)
        for command in commands_to_check
        if isinstance(command, app_commands.Group)
    }
    if actual != EXPECTED_COMMAND_SCHEMA:
        raise RuntimeError(f"refusing readiness: Discord command schema mismatch: {actual!r}")
    return {name: len(children) for name, children in actual.items()}


def _write_readiness(*, guild_id: int, counts: dict[str, int]) -> None:
    configured = os.environ.get("RADARBOT_READY_FILE")
    if not configured:
        return
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"guild_id": guild_id, "commands": counts}, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class RadarBot(commands.Bot):
    """The one shared Discord bot process for both pipelines (CLAUDE.md:
    "共用: ... Discord Bot"). Adds both HouseCog (/house) and AuctionCog
    (/auction) to a single application/token/guild-command-tree sync --
    running either cog's commands from a second, independently-started
    bot process against the same application would race on
    ``tree.sync()`` and intermittently wipe the other pipeline's
    registered commands from the guild.
    """

    def __init__(self) -> None:
        settings = get_settings()
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        engine = create_db_engine(settings.database_url)
        Base.metadata.create_all(engine)
        self.settings = settings
        factory = create_session_factory(engine)
        self.house_service = HouseCommandService(factory)
        self.auction_service = AuctionCommandService(factory)

    async def setup_hook(self) -> None:
        await self.add_cog(HouseCog(self, self.house_service, self.settings.discord_sale_search_channel_id))
        await self.add_cog(
            AuctionCog(self, self.auction_service, self.settings.discord_auction_search_channel_id)
        )
        guild = discord.Object(id=self.settings.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        counts = validate_command_schema(self.tree.get_commands(guild=guild))
        synced = await self.tree.sync(guild=guild)
        if {command.name for command in synced} != set(EXPECTED_COMMAND_SCHEMA):
            raise RuntimeError("refusing readiness: Discord API did not return both command groups")
        _write_readiness(guild_id=self.settings.discord_guild_id, counts=counts)
        logging.info(
            "RadarBot ready; guild=%s house=%s auction=%s",
            self.settings.discord_guild_id,
            counts["house"],
            counts["auction"],
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is required; never commit it to source control")
    token = settings.discord_token
    # container_main loads file-based Docker secrets only after PID 1 has
    # started. Remove the transient environment copies before discord.py
    # opens any subprocess-capable code path; Settings retains the values
    # in process memory for this one bot instance.
    os.environ.pop("DISCORD_TOKEN", None)
    os.environ.pop("DATABASE_URL", None)
    RadarBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
