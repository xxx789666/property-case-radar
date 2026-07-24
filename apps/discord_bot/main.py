import logging

import discord
from discord.ext import commands

from apps.config import get_settings
from apps.discord_bot.cog import HouseCog
from apps.discord_bot.service import HouseCommandService
from database.models import Base
from database.session import create_db_engine, create_session_factory


class RadarBot(commands.Bot):
    def __init__(self) -> None:
        settings = get_settings()
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        engine = create_db_engine(settings.database_url)
        Base.metadata.create_all(engine)
        self.settings = settings
        self.service = HouseCommandService(create_session_factory(engine))

    async def setup_hook(self) -> None:
        await self.add_cog(HouseCog(self, self.service, self.settings.discord_sale_search_channel_id))
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
