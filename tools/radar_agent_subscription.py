from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BROKER_URL_ENV = "RADAR_SUBSCRIPTION_BROKER_URL"
TOKEN_FILE_ENV = "RADAR_SUBSCRIPTION_BROKER_TOKEN_FILE"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar_agent_subscription")
    sub = parser.add_subparsers(dest="command", required=True)
    house = sub.add_parser("house-subscribe")
    house.add_argument("--city", required=True)
    house.add_argument("--district")
    house.add_argument("--max-total-price-twd", type=int, dest="max_total_price_twd")
    house.add_argument("--min-building-area-ping", type=float, dest="min_building_area_ping")
    house.add_argument("--max-age-years", type=float, dest="max_age_years")
    house.add_argument("--min-discount-rate", type=float, dest="min_discount_rate")
    house.add_argument("--property-type", dest="property_type")
    auction = sub.add_parser("auction-subscribe")
    auction.add_argument("--city")
    auction.add_argument("--district")
    auction.add_argument("--case-type", dest="case_type")
    auction.add_argument("--max-floor-price-twd", type=int, dest="max_floor_price_twd")
    auction.add_argument("--min-round", type=int, dest="min_round")
    auction.add_argument(
        "--deliverable",
        dest="require_deliverable",
        choices=("true", "false"),
    )
    auction.add_argument("--min-investment-score", type=float, dest="min_investment_score")
    rental = sub.add_parser("rental-subscribe")
    rental.add_argument("--city", required=True)
    rental.add_argument("--district")
    rental.add_argument(
        "--max-monthly-rent-twd", type=int, dest="max_monthly_rent_twd"
    )
    rental.add_argument("--min-area-ping", type=float, dest="min_area_ping")
    rental.add_argument("--max-area-ping", type=float, dest="max_area_ping")
    rental.add_argument(
        "--rental-type",
        choices=(
            "entire_home",
            "independent_suite",
            "shared_suite",
            "room",
            "parking",
            "other",
        ),
        dest="rental_type",
    )
    rental.add_argument("--layout-contains", dest="layout_contains")
    rental.add_argument("--features-contains", dest="features_contains")
    rental.add_argument("--keyword", dest="keywords_any", action="append")
    rental.add_argument("--min-score", type=int, dest="min_score")
    sub.add_parser("list")
    cancel = sub.add_parser("cancel")
    cancel.add_argument(
        "--kind", choices=("house", "auction", "rental"), required=True
    )
    cancel.add_argument("--id", type=int, required=True)
    return parser


def _payload(args: argparse.Namespace) -> dict:
    if args.command == "list":
        return {"operation": "list"}
    if args.command == "cancel":
        return {"operation": "cancel", "kind": args.kind, "id": args.id}
    operation = {
        "house-subscribe": "house-create",
        "auction-subscribe": "auction-create",
        "rental-subscribe": "rental-create",
    }[args.command]
    values = {
        key: value
        for key, value in vars(args).items()
        if key != "command" and value is not None
    }
    if "require_deliverable" in values:
        values["require_deliverable"] = values["require_deliverable"] == "true"
    return {"operation": operation, **values}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    url = os.environ.get(BROKER_URL_ENV, "http://127.0.0.1:18767").rstrip("/")
    token_file = os.environ.get(TOKEN_FILE_ENV)
    if not token_file:
        print('{"error":"subscription broker token is not configured"}')
        return 2
    try:
        token = Path(token_file).read_text(encoding="ascii").strip()
        request = urllib.request.Request(
            f"{url}/v1/subscriptions",
            data=json.dumps(_payload(args)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            sys.stdout.write(response.read().decode("utf-8") + "\n")
        return 0
    except urllib.error.HTTPError as error:
        sys.stdout.write(error.read().decode("utf-8") + "\n")
        return 1
    except OSError:
        print('{"error":"subscription broker is unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
