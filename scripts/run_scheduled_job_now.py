#!/usr/bin/env python3
"""Run one production sale or rental scheduler job immediately."""

from __future__ import annotations

import argparse
import logging

from apps.config import get_settings
from apps.scheduler.main import make_live_rental_job, make_live_sale_job
from apps.services.system_alerts import update_system_alert
from crawlers.rental.captured_source import CapturedRentalCrawler
from crawlers.rental.captured_status import CapturedRentalStatusVerifier
from crawlers.sale.captured_source import CapturedSaleCrawler
from crawlers.sale.captured_status import CapturedSaleStatusVerifier
from crawlers.sale.composite import CompositeSaleCrawler
from crawlers.sale.housefun_source import HousefunSaleCrawler, HousefunStatusVerifier
from database.session import create_db_engine, create_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=("rental", "sale"))
    return parser


def run_rental() -> None:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        crawler = CapturedRentalCrawler(
            settings.rental_capture_script,
            output_dir=settings.rental_capture_output_dir,
            max_pages=settings.rental_capture_max_pages,
            other_max_pages=settings.rental_capture_other_max_pages,
            focus_max_pages=settings.rental_capture_focus_max_pages,
            workers=settings.rental_capture_workers,
            json_retention_days=settings.rental_capture_json_retention_days,
        )
        make_live_rental_job(
            factory,
            crawler,
            settings,
            CapturedRentalStatusVerifier(settings.rental_capture_script),
        )()
    finally:
        engine.dispose()


def run_sale() -> None:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        sources = {
            "591": CapturedSaleCrawler(
                settings.sale_capture_script,
                output_dir=settings.sale_capture_output_dir,
                max_pages=settings.sale_capture_max_pages,
                workers=settings.sale_capture_workers,
                json_retention_days=settings.sale_capture_json_retention_days,
            )
        }
        additional_status_verifiers = ()
        if settings.housefun_capture_enabled:
            sources["housefun"] = HousefunSaleCrawler(
                max_pages=settings.housefun_capture_max_pages
            )
            additional_status_verifiers = (
                (
                    "housefun",
                    HousefunStatusVerifier(),
                    settings.housefun_status_verify_limit,
                ),
            )
        make_live_sale_job(
            factory,
            CompositeSaleCrawler(sources),
            settings,
            CapturedSaleStatusVerifier(settings.sale_capture_script),
            additional_status_verifiers,
        )()
    finally:
        engine.dispose()


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    try:
        if args.job == "rental":
            run_rental()
        else:
            run_sale()
    except Exception as error:
        update_system_alert(
            settings,
            key=f"scheduler-job:{args.job}-crawler",
            failing=True,
            title=f"排程工作 {args.job}-crawler",
            detail=str(error)[:1500],
        )
        raise
    update_system_alert(
        settings,
        key=f"scheduler-job:{args.job}-crawler",
        failing=False,
        title=f"排程工作 {args.job}-crawler",
        detail=f"{args.job}-crawler 手動補跑已正常完成",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
