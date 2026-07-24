from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://radar:change-me@localhost:5432/radar"
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

    @model_validator(mode="after")
    def validate_delay_range(self) -> "Settings":
        if self.crawler_max_delay_seconds < self.crawler_min_delay_seconds:
            raise ValueError("crawler max delay must be >= min delay")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
