from crawlers.sale.housefun_source import parse_housefun_list_page


def test_housefun_public_card_parser() -> None:
    html = """
    <section class="m-list-obj">
      <a class="m-list-figure" href="/buy/house/6754608"></a>
      <div class="casename"><a href="/buy/house/6754608">中壢住宅用建地</a></div>
      <address class="address">桃園市中壢區中央西路</address>
      <div class="ping-pattern">
        <span class="ping-number"><em class="number">100.5</em></span>
        <em class="pattern">土地</em>
      </div>
      <div class="price">
        <a class="discount-price"><em class="number">1,500</em></a>
      </div>
    </section>
    """
    items = parse_housefun_list_page(html)
    assert len(items) == 1
    item = items[0]
    assert item.source == "housefun"
    assert item.source_property_id == "6754608"
    assert item.city == "桃園市"
    assert item.district == "中壢區"
    assert item.total_price_twd == 15_000_000
    assert item.building_type == "土地"
    assert item.usage == "建地"
