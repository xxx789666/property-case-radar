import logging

import discord
from discord.ext import commands

from apps.config import get_settings
from apps.discord_bot.auction_cog import AuctionCog
from apps.discord_bot.auction_service import AuctionCommandService
from apps.discord_bot.cog import HouseCog
from apps.discord_bot.service import HouseCommandService
from database.models import Base
from database.session import create_db_engine, create_session_factory


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
        await self.tree.sync(guild=guild)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is required; never commit it to source control")
    RadarBot().run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
