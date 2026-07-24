"""Taiwan Real Estate Case Radar.

Package root shared by the two independent pipelines described in
taiwan_real_estate_radar.md:

- ``property_case_radar.sale``    (regular for-sale listings; owned by
  the sale-pipeline workstream, not present in this worktree yet)
- ``property_case_radar.auction`` (court foreclosure auction cases; this
  worktree)
- ``property_case_radar.shared``  (thin integration points shared by both
  pipelines, e.g. regional market price lookups)
"""
