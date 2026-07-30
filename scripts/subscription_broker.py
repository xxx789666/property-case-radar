from __future__ import annotations

import hmac
import json
import secrets
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sqlalchemy import select

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.config import get_settings
from database.models.auction import AuctionSubscription, CaseType
from database.models.sale import PropertySubscription
from database.models.rental import RentalSubscription
from database.session import create_db_engine, create_session_factory

HOST = "127.0.0.1"
PORT = 18767
MAX_BODY_BYTES = 16 * 1024
ALLOWED_USER_ID = 843428445802725388
PROPERTY_TYPES = {
    "house",
    "land",
    "farmland",
    "building_land",
    "residential_land",
    "commercial_land",
    "industrial_land",
    "type_a_building_land",
    "type_b_building_land",
    "type_c_building_land",
    "type_d_building_land",
    "forest_land",
    "hillside_land",
    "road_land",
}
RENTAL_TYPES = {
    "entire_home",
    "independent_suite",
    "shared_suite",
    "room",
    "parking",
    "other",
}


class RequestError(ValueError):
    pass


def _text(value: Any, name: str, *, maximum: int = 32) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result or len(result) > maximum:
        raise RequestError(f"{name} is invalid")
    return result


def _text_list(
    value: Any,
    name: str,
    *,
    maximum_items: int = 5,
    maximum_item_length: int = 16,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
        raise RequestError(f"{name} is invalid")
    items: list[str] = []
    for raw in value:
        item = str(raw).strip()
        if (
            not item
            or len(item) > maximum_item_length
            or "、" in item
            or item in items
        ):
            raise RequestError(f"{name} is invalid")
        items.append(item)
    return "、".join(items)


def _integer(
    value: Any, name: str, *, minimum: int = 0, maximum: int = 10**12
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RequestError(f"{name} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise RequestError(f"{name} is invalid") from error
    if result < minimum or result > maximum:
        raise RequestError(f"{name} is out of range")
    return result


def _decimal(value: Any, name: str, *, minimum: float, maximum: float) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise RequestError(f"{name} is invalid") from error
    if result < Decimal(str(minimum)) or result > Decimal(str(maximum)):
        raise RequestError(f"{name} is out of range")
    return result


@dataclass(frozen=True)
class BrokerResult:
    operation: str
    subscription: dict[str, Any] | None = None
    subscriptions: list[dict[str, Any]] | None = None
    changed: bool | None = None


class SubscriptionBroker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.engine = create_db_engine(self.settings.database_url)
        self.factory = create_session_factory(self.engine)

    @staticmethod
    def _sale_json(item: PropertySubscription) -> dict[str, Any]:
        return {
            "kind": "house",
            "id": item.id,
            "city": item.city,
            "district": item.district,
            "max_total_price_twd": item.max_total_price_twd,
            "min_building_area_ping": (
                float(item.min_building_area_ping)
                if item.min_building_area_ping is not None
                else None
            ),
            "max_age_years": (
                float(item.max_age_years) if item.max_age_years is not None else None
            ),
            "min_discount_rate": (
                float(item.min_discount_rate)
                if item.min_discount_rate is not None
                else None
            ),
            "property_type": item.property_type,
        }

    @staticmethod
    def _auction_json(item: AuctionSubscription) -> dict[str, Any]:
        return {
            "kind": "auction",
            "id": item.id,
            "city": item.city,
            "district": item.district,
            "case_type": item.case_type.value if item.case_type else None,
            "max_floor_price_twd": item.max_floor_price_twd,
            "min_round": item.min_round,
            "require_deliverable": item.require_deliverable,
            "min_investment_score": (
                float(item.min_investment_score)
                if item.min_investment_score is not None
                else None
            ),
        }

    @staticmethod
    def _rental_json(item: RentalSubscription) -> dict[str, Any]:
        return {
            "kind": "rental",
            "id": item.id,
            "city": item.city,
            "district": item.district,
            "max_monthly_rent_twd": item.max_monthly_rent_twd,
            "min_area_ping": (
                float(item.min_area_ping)
                if item.min_area_ping is not None
                else None
            ),
            "max_area_ping": (
                float(item.max_area_ping)
                if item.max_area_ping is not None
                else None
            ),
            "rental_type": item.rental_type,
            "layout_contains": item.layout_contains,
            "features_contains": item.features_contains,
            "keywords_any": (
                item.keywords_any.split("、") if item.keywords_any else None
            ),
            "min_score": item.min_score,
        }

    def execute(self, payload: dict[str, Any]) -> BrokerResult:
        operation = payload.get("operation")
        with self.factory() as session:
            if operation == "list":
                sales = session.scalars(
                    select(PropertySubscription).where(
                        PropertySubscription.discord_user_id == ALLOWED_USER_ID,
                        PropertySubscription.active.is_(True),
                    )
                ).all()
                auctions = session.scalars(
                    select(AuctionSubscription).where(
                        AuctionSubscription.discord_user_id == ALLOWED_USER_ID,
                        AuctionSubscription.active.is_(True),
                    )
                ).all()
                rentals = session.scalars(
                    select(RentalSubscription).where(
                        RentalSubscription.discord_user_id == ALLOWED_USER_ID,
                        RentalSubscription.active.is_(True),
                    )
                ).all()
                return BrokerResult(
                    operation="list",
                    subscriptions=[
                        *(self._sale_json(item) for item in sales),
                        *(self._auction_json(item) for item in auctions),
                        *(self._rental_json(item) for item in rentals),
                    ],
                )
            if operation == "cancel":
                kind = payload.get("kind")
                subscription_id = _integer(
                    payload.get("id"), "id", minimum=1, maximum=2**31 - 1
                )
                models = {
                    "house": PropertySubscription,
                    "auction": AuctionSubscription,
                    "rental": RentalSubscription,
                }
                model = models.get(kind)
                if model is None:
                    raise RequestError("kind must be house, auction, or rental")
                item = session.scalar(
                    select(model).where(
                        model.id == subscription_id,
                        model.discord_user_id == ALLOWED_USER_ID,
                        model.active.is_(True),
                    )
                )
                if item is None:
                    return BrokerResult(operation="cancel", changed=False)
                item.active = False
                session.commit()
                return BrokerResult(operation="cancel", changed=True)
            if operation == "house-create":
                return self._create_house(session, payload)
            if operation == "auction-create":
                return self._create_auction(session, payload)
            if operation == "rental-create":
                return self._create_rental(session, payload)
            raise RequestError("unsupported operation")

    def _create_house(self, session, payload: dict[str, Any]) -> BrokerResult:
        property_type = _text(payload.get("property_type"), "property_type")
        if property_type is not None and property_type not in PROPERTY_TYPES:
            raise RequestError("property_type is invalid")
        values = {
            "discord_user_id": ALLOWED_USER_ID,
            "city": _text(payload.get("city"), "city"),
            "district": _text(payload.get("district"), "district"),
            "max_total_price_twd": _integer(
                payload.get("max_total_price_twd"), "max_total_price_twd", minimum=1
            ),
            "min_building_area_ping": _decimal(
                payload.get("min_building_area_ping"),
                "min_building_area_ping",
                minimum=0.01,
                maximum=100000,
            ),
            "max_age_years": _decimal(
                payload.get("max_age_years"), "max_age_years", minimum=0, maximum=500
            ),
            "min_discount_rate": _decimal(
                payload.get("min_discount_rate"),
                "min_discount_rate",
                minimum=-10,
                maximum=1,
            ),
            "property_type": property_type,
            "channel_id": self.settings.discord_sale_search_channel_id,
        }
        if not values["city"]:
            raise RequestError("city is required")
        return self._insert_unique(session, PropertySubscription, values, self._sale_json)

    def _create_auction(self, session, payload: dict[str, Any]) -> BrokerResult:
        case_type_text = _text(payload.get("case_type"), "case_type")
        try:
            case_type = CaseType(case_type_text) if case_type_text else None
        except ValueError as error:
            raise RequestError("case_type is invalid") from error
        deliverable = payload.get("require_deliverable")
        if deliverable is not None and not isinstance(deliverable, bool):
            raise RequestError("require_deliverable must be boolean")
        values = {
            "discord_user_id": ALLOWED_USER_ID,
            "city": _text(payload.get("city"), "city"),
            "district": _text(payload.get("district"), "district"),
            "case_type": case_type,
            "max_floor_price_twd": _integer(
                payload.get("max_floor_price_twd"), "max_floor_price_twd", minimum=1
            ),
            "min_round": _integer(
                payload.get("min_round"), "min_round", minimum=1, maximum=99
            ),
            "require_deliverable": deliverable,
            "min_investment_score": _decimal(
                payload.get("min_investment_score"),
                "min_investment_score",
                minimum=0,
                maximum=100,
            ),
            "channel_id": self.settings.discord_auction_search_channel_id,
        }
        if not any(value is not None for key, value in values.items() if key not in {"discord_user_id", "channel_id"}):
            raise RequestError("at least one auction condition is required")
        return self._insert_unique(session, AuctionSubscription, values, self._auction_json)

    def _create_rental(self, session, payload: dict[str, Any]) -> BrokerResult:
        rental_type = _text(payload.get("rental_type"), "rental_type")
        if rental_type is not None and rental_type not in RENTAL_TYPES:
            raise RequestError("rental_type is invalid")
        min_area = _decimal(
            payload.get("min_area_ping"),
            "min_area_ping",
            minimum=0.01,
            maximum=100000,
        )
        max_area = _decimal(
            payload.get("max_area_ping"),
            "max_area_ping",
            minimum=0.01,
            maximum=100000,
        )
        if min_area is not None and max_area is not None and min_area > max_area:
            raise RequestError("min_area_ping must not exceed max_area_ping")
        values = {
            "discord_user_id": ALLOWED_USER_ID,
            "city": _text(payload.get("city"), "city"),
            "district": _text(payload.get("district"), "district"),
            "max_monthly_rent_twd": _integer(
                payload.get("max_monthly_rent_twd"),
                "max_monthly_rent_twd",
                minimum=1,
            ),
            "min_area_ping": min_area,
            "max_area_ping": max_area,
            "rental_type": rental_type,
            "layout_contains": _text(
                payload.get("layout_contains"), "layout_contains"
            ),
            "features_contains": _text(
                payload.get("features_contains"),
                "features_contains",
                maximum=64,
            ),
            "keywords_any": _text_list(
                payload.get("keywords_any"), "keywords_any"
            ),
            "min_score": _integer(
                payload.get("min_score"), "min_score", minimum=0, maximum=100
            ),
            "channel_id": self.settings.discord_rental_search_channel_id,
        }
        if not values["city"]:
            raise RequestError("city is required")
        return self._insert_unique(
            session, RentalSubscription, values, self._rental_json
        )

    def _insert_unique(self, session, model, values, serializer) -> BrokerResult:
        active_count = len(
            session.scalars(
                select(model).where(
                    model.discord_user_id == ALLOWED_USER_ID,
                    model.active.is_(True),
                )
            ).all()
        )
        if active_count >= 20:
            raise RequestError("active subscription limit reached")
        conditions = [
            getattr(model, key) == value
            for key, value in values.items()
            if key != "channel_id"
        ]
        existing = session.scalar(
            select(model).where(*conditions, model.active.is_(True))
        )
        if existing is not None:
            return BrokerResult(
                operation="already-exists", subscription=serializer(existing)
            )
        item = model(**values)
        session.add(item)
        session.commit()
        session.refresh(item)
        return BrokerResult(operation="created", subscription=serializer(item))


def ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="ascii")
    if len(token) < 32:
        raise RuntimeError("subscription broker token is invalid")
    return token


class Handler(BaseHTTPRequestHandler):
    broker: SubscriptionBroker
    token: str

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/subscriptions":
            self._json(404, {"error": "not found"})
            return
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, self.token):
            self._json(403, {"error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise RequestError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise RequestError("request must be an object")
            result = self.broker.execute(payload)
            self._json(200, asdict(result))
        except (RequestError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception:
            self._json(500, {"error": "subscription operation failed"})


def main() -> None:
    settings = get_settings()
    token_path = REPOSITORY_ROOT / "openab" / ".local" / "subscription_broker_token"
    Handler.token = ensure_token(token_path)
    Handler.broker = SubscriptionBroker()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"subscription broker listening on {HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        Handler.broker.engine.dispose()


if __name__ == "__main__":
    main()
