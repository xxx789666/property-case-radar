"""Court foreclosure auction ("法拍屋") pipeline.

Independent from the sale-listing pipeline per taiwan_real_estate_radar.md
section 十七: separate data source, fields, scoring, status tracking, and
Discord commands. Shares only: PostgreSQL (eventually), 內政部實價登錄 data
(via property_case_radar.shared.market_prices), the Discord bot process,
notification delivery, and user subscription mechanics at a conceptual
level (this vertical keeps its own auction_subscriptions model).
"""
