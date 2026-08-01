from functools import lru_cache
from pathlib import Path

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
    discord_rental_new_channel_id: int = 1532276339405492304
    discord_rental_price_drop_channel_id: int = 1532277198663319674
    discord_rental_high_score_channel_id: int = 1532277201045684356
    discord_rental_search_channel_id: int = 1532282082854830202
    rental_scheduler_daily_hour: int = Field(default=11, ge=0, le=23)
    rental_scheduler_daily_minute: int = Field(default=0, ge=0, le=59)
    rental_capture_enabled: bool = True
    rental_capture_script: Path = Path("scripts/capture_rental_results.py")
    rental_capture_output_dir: Path = Path(r"D:\網頁識別認證\rental_json")
    rental_capture_max_pages: int = Field(default=3, ge=1, le=20)
    rental_capture_workers: int = Field(default=4, ge=1, le=8)
    rental_capture_json_retention_days: int = Field(default=30, ge=1, le=365)
    rental_high_score_threshold: int = Field(default=80, ge=0, le=100)
    rental_status_missing_days: int = Field(default=3, ge=1, le=30)
    rental_status_verify_limit: int = Field(default=500, ge=1, le=2000)
    discord_system_alert_channel_id: int = 1530072733818556541
    sale_scheduler_daily_hour: int = Field(default=12, ge=0, le=23)
    sale_scheduler_daily_minute: int = Field(default=0, ge=0, le=59)
    scheduler_daily_hour: int = Field(default=13, ge=0, le=23)
    scheduler_daily_minute: int = Field(default=0, ge=0, le=59)
    sale_crawl_interval_minutes: int = Field(default=1440, ge=1440, le=10080)
    sale_high_score_threshold: int = Field(default=80, ge=0, le=100)
    sale_capture_enabled: bool = True
    sale_capture_script: Path = Path("scripts/capture_sale_results.py")
    sale_capture_output_dir: Path = Path(r"D:\網頁識別認證\sale_json")
    sale_capture_max_pages: int = Field(default=3, ge=1, le=20)
    sale_capture_workers: int = Field(default=4, ge=1, le=8)
    sale_capture_json_retention_days: int = Field(default=30, ge=1, le=365)
    housefun_capture_enabled: bool = True
    housefun_capture_max_pages: int = Field(default=3, ge=1, le=3)
    housefun_status_verify_limit: int = Field(default=50, ge=1, le=200)
    sale_status_missing_days: int = Field(default=3, ge=1, le=30)
    sale_status_verify_limit: int = Field(default=500, ge=1, le=2000)
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
    # 法拍公告爬蟲每天一次；允許營運端調低頻率，但不可高於每日一次。
    auction_crawl_interval_hours: int = Field(default=24, ge=24, le=168)
    auction_high_score_threshold: int = Field(default=80, ge=0, le=100)
    auction_upcoming_within_days: int = Field(default=7, ge=1, le=30)
    auction_capture_enabled: bool = True
    auction_capture_script: Path = Path(r"D:\網頁識別認證\capture_auction_results.py")
    auction_capture_output_dir: Path = Path(r"D:\網頁識別認證\json")
    auction_capture_download_dir: Path = Path(r"D:\網頁識別認證\downloads")
    auction_capture_ocr_url: str = "http://127.0.0.1:1224/api/ocr"
    auction_capture_ocr_executable: Path = Path(
        r"D:\Umi-OCR_Paddle_v2.1.5\Umi-OCR.exe"
    )
    auction_capture_ocr_startup_timeout_seconds: int = Field(default=45, ge=5, le=300)
    auction_capture_json_retention_days: int = Field(default=30, ge=1, le=365)
    auction_failed_retry_delay_minutes: int = Field(default=15, ge=1, le=120)
    auction_failed_retry_rounds: int = Field(default=3, ge=0, le=10)

    database_backup_dir: Path = Path(
        r"D:\網頁識別認證\backups\postgres"
    )
    database_backup_retention_days: int = Field(default=14, ge=1, le=365)
    system_alert_state_path: Path = Path("logs/system-alert-state.json")

    # Official MOI actual-price current-batch sync.  The Judicial Yuan
    # The MOJ auction capture runs separately through the configured local
    # Playwright script; this remains the official open-data price source.
    market_sync_interval_hours: int = Field(default=24, ge=6, le=168)
    moi_cache_dir: Path = Path(r"D:\網頁識別認證\moi_cache")
    moi_history_years: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_delay_range(self) -> "Settings":
        if self.crawler_max_delay_seconds < self.crawler_min_delay_seconds:
            raise ValueError("crawler max delay must be >= min delay")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
