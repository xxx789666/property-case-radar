from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.config import Settings
from crawlers.sale.base import SaleListing
from database.repositories.sale import PropertyRepository


def test_health_and_property_api(tmp_path) -> None:
    database_path = tmp_path / "api.db"
    app = create_app(Settings(database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            PropertyRepository(session).upsert_listing(
                SaleListing(
                    source="fixture",
                    source_property_id="api-p1",
                    url="https://example.invalid/api-p1",
                    city="桃園市",
                    district="中壢區",
                    total_price_twd=12_800_000,
                    unit_price_per_ping_twd=351_000,
                    building_area_ping=Decimal("36.5"),
                    discount_rate=Decimal("0.1091"),
                    score=82,
                )
            )
            session.commit()

        assert client.get("/health").json() == {"status": "ok"}
        response = client.get(
            "/api/v1/properties",
            params={"city": "桃園市", "max_price_wan": 1500, "min_discount_percent": 10},
        )
        assert response.status_code == 200
        assert response.json()[0]["source_property_id"] == "api-p1"
        property_id = response.json()[0]["id"]
        assert client.get(f"/api/v1/properties/{property_id}").status_code == 200
        assert client.get("/api/v1/properties/99999").status_code == 404
