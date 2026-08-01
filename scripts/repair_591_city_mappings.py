"""Repair rows imported while three 591 region IDs were mapped cyclically."""

from __future__ import annotations

import json

from sqlalchemy import select

from apps.config import get_settings
from database.models.sale import Property
from database.session import create_db_engine, create_session_factory


YUNLIN_DISTRICTS = {
    "斗六市", "斗南鎮", "虎尾鎮", "西螺鎮", "土庫鎮", "北港鎮",
    "古坑鄉", "大埤鄉", "莿桐鄉", "林內鄉", "二崙鄉", "崙背鄉",
    "麥寮鄉", "東勢鄉", "褒忠鄉", "臺西鄉", "元長鄉", "四湖鄉",
    "口湖鄉", "水林鄉",
}
CHIAYI_CITY_DISTRICTS = {"東區", "西區"}
CHIAYI_COUNTY_DISTRICTS = {
    "太保市", "朴子市", "布袋鎮", "大林鎮", "民雄鄉", "溪口鄉",
    "新港鄉", "六腳鄉", "東石鄉", "義竹鄉", "鹿草鄉", "水上鄉",
    "中埔鄉", "竹崎鄉", "梅山鄉", "番路鄉", "大埔鄉", "阿里山鄉",
}


def main() -> int:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    repaired = {"雲林縣": 0, "嘉義市": 0, "嘉義縣": 0}

    with factory() as session:
        rows = session.scalars(
            select(Property).where(
                Property.source == "591",
                Property.city.in_(("雲林縣", "嘉義市", "嘉義縣")),
            )
        ).all()
        for row in rows:
            target_city: str | None = None
            if row.city == "雲林縣" and row.district in CHIAYI_CITY_DISTRICTS:
                target_city = "嘉義市"
            elif row.city == "嘉義市" and row.district in CHIAYI_COUNTY_DISTRICTS:
                target_city = "嘉義縣"
            elif row.city == "嘉義縣" and row.district in YUNLIN_DISTRICTS:
                target_city = "雲林縣"
            if target_city:
                row.city = target_city
                repaired[target_city] += 1
        session.commit()

    print(json.dumps({"status": "ok", "repaired": repaired}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
