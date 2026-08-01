# Data source coverage

Verified on 2026-07-28.

| Source | Status | Access rule |
|---|---|---|
| 591 public sale pages | Active | Operator-approved capture, newest 3 pages |
| HouseFun public buy pages | Active | Direct public HTML, robots checked on every run, newest 3 pages, 2-second minimum delay |
| Sinyi sale pages | Disabled | Its service terms prohibit non-human extraction, automatic link clicks, and repeated URL reads |
| MOJ Administrative Enforcement Agency | Active | Operator-approved official capture |
| Judicial Yuan court auctions | Disabled | Current auction host publishes `robots.txt` with `Disallow: /`; the former open pending-auction dataset was permanently retired |

The disabled sources must not be enabled by changing selectors or user agents.
They require an authorized API, a reusable licensed data feed, or written
permission that explicitly permits this automation.

Primary references:

- Sinyi service terms: https://www.sinyi.com.tw/tos
- Judicial Yuan auction entry: https://www.judicial.gov.tw/tw/np-135-1.html
- Judicial Yuan former open-data announcement:
  https://www.judicial.gov.tw/tw/cp-1429-67516-50b2a-1.html
- Government Data Platform retirement notice:
  https://data.gov.tw/news/23843
