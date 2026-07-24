"""Discord-facing layer for the auction vertical.

commands.py implements the /auction slash commands as plain,
framework-agnostic Python (no discord.py import) so they can be
unit-tested with fakes and mocks -- per this round's instructions, we do
not stand up a live Discord connection. A thin discord.py adapter
(mapping ``discord.Interaction`` -> the calls in commands.py, and
posting the resulting text via ``interaction.response.send_message``)
is the natural next step but is intentionally not included here; see
docs/auction_pipeline.md.
"""
