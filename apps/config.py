from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://radar:change-me@127.0.0.1:15432/radar"
    discord_token: str | None = None
    discord_guild_id: int = 1530072733818556538
    discord_sale_new_channel_id: int = 1530073991442595880
    discord_sale_price_drop_channel_id: int = 1530075359490609202
    discord_sale_high_score_channel_id: int = 1530075382873587855
    discord_sale_search_channel_id: int = 1530076451242508318
    sale_crawl_interval_minutes: int = Field(default=90, ge=60, le=120)
    sale_high_score_threshold: int = Field(default=80, ge=0, le=100)
    crawler_min_delay_seconds: float = Field(default=2, ge=0)
    crawler_max_delay_seconds: float = Field(default=5, ge=0)

    # 法拍屋 pipeline -- independent Discord channels/schedule/threshold
    # per CLAUDE.md ("獨立: ... Discord 指令"), sharing only the bot
    # process, DB, and notification/subscription plumbing with the sale
    # settings above.
    discord_auction_new_channel_id: int = 1530075570140876982
    discord_auction_upcoming_channel_id: int = 1530075622636781639
    discord_auction_round_channel_id: int = 1530075673052577922
    discord_auction_high_score_channel_id: int = 1530075701150089409
    discord_auction_suspended_channel_id: int = 1530075739750268989
    discord_auction_search_channel_id: int = 1530076529751756870
    # spec section 十二: 法拍公告爬蟲每天 2～4 次 -> an interval of 6-12h.
    auction_crawl_interval_hours: int = Field(default=8, ge=6, le=12)
    auction_high_score_threshold: int = Field(default=80, ge=0, le=100)
    auction_upcoming_within_days: int = Field(default=7, ge=1, le=30)

    @model_validator(mode="after")
    def validate_delay_range(self) -> "Settings":
        if self.crawler_max_delay_seconds < self.crawler_min_delay_seconds:
            raise ValueError("crawler max delay must be >= min delay")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
