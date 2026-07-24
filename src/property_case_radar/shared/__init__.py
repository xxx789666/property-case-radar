"""Integration points shared between the sale and auction pipelines.

Per taiwan_real_estate_radar.md section 十一 / 十七, both pipelines read
from a single ``market_prices`` table (populated from 內政部實價登錄) but
otherwise keep independent schemas, scoring, and Discord commands. This
subpackage holds the *minimal* contracts the auction vertical needs from
that shared infrastructure, so it can be developed and tested without
depending on however the shared DB/API layer is ultimately implemented.
"""
