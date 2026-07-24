"""Court announcement fetch layer.

COMPLIANCE (taiwan_real_estate_radar.md section 十六):
  * 需確認網站使用條款 -- confirm each source's terms of service before
    adding a live implementation.
  * 避免高頻請求 -- see RateLimiter in base.py.
  * 建議加入隨機延遲與重試 -- see RateLimiter.delay_seconds().
  * 優先使用公開 API 或公開頁面 -- only fetch public announcement pages.
  * 不應繞過登入、付費牆或安全機制 -- no login bypass, no anti-bot
    evasion, ever.

This round intentionally ships only a fixture-backed source
(fixture_source.py) that reads local HTML files -- no outbound network
calls. A live implementation (httpx/Playwright, per spec section 十三)
can be added later as an additional module satisfying the same
``AnnouncementSource`` protocol in base.py; it must keep the compliance
constraints above and should be reviewed before being pointed at a real
court site.
"""
