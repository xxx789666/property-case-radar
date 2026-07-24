from collections.abc import Iterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.config import Settings, get_settings
from database.models import Base, Property
from database.repositories.sale import PropertyRepository, PropertySearch
from database.session import create_db_engine, create_session_factory


class PropertyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: str
    source_property_id: str
    url: str
    city: str
    district: str
    total_price_twd: int
    unit_price_per_ping_twd: int
    building_area_ping: Decimal
    market_unit_price_twd: int | None
    discount_rate: Decimal | None
    score: int | None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        yield

    app = FastAPI(
        title="台灣房地產案件雷達 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.session_factory = factory

    def get_session() -> Iterator[Session]:
        with factory() as session:
            yield session

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/v1/properties", response_model=list[PropertyOut])
    def search_properties(
        city: str | None = None,
        district: str | None = None,
        max_price_wan: int | None = Query(default=None, ge=1),
        min_area_ping: Decimal | None = Query(default=None, gt=0),
        max_age_years: Decimal | None = Query(default=None, ge=0),
        min_discount_percent: Decimal | None = Query(default=None, ge=-100, le=100),
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> list[Property]:
        return PropertyRepository(session).search(
            PropertySearch(
                city=city,
                district=district,
                max_total_price_twd=max_price_wan * 10_000 if max_price_wan else None,
                min_building_area_ping=min_area_ping,
                max_age_years=max_age_years,
                min_discount_rate=min_discount_percent / 100 if min_discount_percent is not None else None,
                limit=limit,
            )
        )

    @app.get("/api/v1/properties/{property_id}", response_model=PropertyOut)
    def property_detail(property_id: int, session: Session = Depends(get_session)) -> Property:
        item = PropertyRepository(session).get(property_id)
        if item is None:
            raise HTTPException(status_code=404, detail="property not found")
        return item

    return app


app = create_app()
