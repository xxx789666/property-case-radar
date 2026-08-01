#!/usr/bin/env python3
"""Render ``/etc/openab/config.toml`` (the checked-in, secret-free template
mounted read-only into the container) into a runtime config with the
Discord bot token substituted in, and fail closed before OpenAB ever
touches the network if anything about the result looks wrong.

This is the GATEWAY container. It runs as one fixed unprivileged user
throughout (no root at any point -- see openab/README.md's "least
privilege" section), so the rendered file's 0600 permission bit is not
what keeps the spawned ``bridge-client.mjs`` child from reading it back
(same UID means it could open the same path). The real mitigation is
``container-entrypoint.sh`` deleting this file shortly after starting
``openab``, before the Discord adapter can accept a message and therefore
before any ``bridge-client.mjs`` child can possibly be spawned; 0600 is
still applied as a cheap extra layer against anything else that might
share the filesystem. ``bridge-client.mjs`` itself never has any reason to
read this file at all -- it is a pure stdio<->TCP relay to the codex-acp
sidecar container (see openab/sidecar/), not codex-acp itself, and never
touches Discord tokens or database credentials.

Mirrors the design of the sibling ``discord-personal-assistant`` project's
``openab/credit-report/runtime-config.mjs`` (exact placeholder-count
checks, atomic write-then-rename) but in Python for this one script --
this repository's own application code has no Node.js runtime dependency,
though this container also ships Node (already present in the upstream
OpenAB base image) to run bridge-client.mjs. The template here only has
one secret placeholder (the bot token; the channel allowlist is fixed by
discord 伺服器.txt and is not a secret, so it is hardcoded directly in the
committed template instead of being another runtime placeholder).

Run as ``python3 runtime_config.py TEMPLATE OUTPUT`` by
``container-entrypoint.sh``, before ``openab`` starts.
"""

from __future__ import annotations

import os
import json
import sys
import tomllib
from pathlib import Path

TOKEN_PLACEHOLDER = '"${OPENAB_DISCORD_BOT_TOKEN}"'
TOKEN_ENV_VAR = "OPENAB_DISCORD_BOT_TOKEN"

# Fixed by discord 伺服器.txt (CLAUDE.md: treat these as environment-specific
# configuration, not secrets) -- the same three private search channels
# apps/discord_bot/main.py's RadarBot already scopes /house and /auction
# search to. Never sourced from an env var: an env var could be misconfigured
# at deploy time and silently widen the allowlist to a public/broadcast
# channel, defeating the whole point of pinning this in code.
EXPECTED_ALLOWED_CHANNELS = frozenset(
    {
        "1530076451242508318",
        "1530076529751756870",
        "1532282082854830202",
    }
)


class ConfigError(RuntimeError):
    pass


def _replace_discord_allowed_users(template: str, allowed_users: list[str]) -> str:
    if not allowed_users or not all(
        isinstance(user_id, str) and user_id.isdigit() for user_id in allowed_users
    ):
        raise ConfigError("runtime allowed-users file must contain a non-empty list of numeric IDs")

    lines = template.splitlines(keepends=True)
    in_discord = False
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_discord = stripped == "[discord]"
        elif in_discord and stripped.startswith("allowed_users"):
            if stripped != "allowed_users = [":
                raise ConfigError("discord.allowed_users must use the expected multiline TOML form")
            start = index
            for candidate in range(index + 1, len(lines)):
                if lines[candidate].strip() == "]":
                    end = candidate
                    break
            break
    if start is None or end is None:
        raise ConfigError("discord.allowed_users block was not found")

    newline = "\r\n" if "\r\n" in template else "\n"
    replacement = [f"allowed_users = [{newline}"]
    replacement.extend(f'  "{user_id}",{newline}' for user_id in sorted(set(allowed_users)))
    replacement.append(f"]{newline}")
    lines[start : end + 1] = replacement
    return "".join(lines)


def render_config_text(
    template: str,
    *,
    token: str | None,
    allowed_users: list[str] | None = None,
) -> str:
    if not token or any(ch.isspace() for ch in token):
        raise ConfigError(f"{TOKEN_ENV_VAR} is missing or contains invalid whitespace")

    if allowed_users is not None:
        template = _replace_discord_allowed_users(template, allowed_users)

    count = template.count(TOKEN_PLACEHOLDER)
    if count != 1:
        raise ConfigError(f"template must contain exactly one {TOKEN_PLACEHOLDER} placeholder, found {count}")

    # TOML string escaping: token characters from Discord's own token
    # alphabet (base64url + '.') never require escaping, but re-encode via
    # a real TOML/JSON string literal rather than naive interpolation so a
    # future token format change can't produce an unparsable or
    # (worse) syntactically-injected config.
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    rendered = template.replace(TOKEN_PLACEHOLDER, f'"{escaped}"')

    if TOKEN_PLACEHOLDER in rendered:
        raise ConfigError("token placeholder still present after substitution")

    # Other ${VAR} placeholders (e.g. [agent].env's ACP_SIDECAR_HOST/
    # ACP_SIDECAR_PORT -- non-secret, just the sidecar's address on the
    # internal Compose network) are intentionally left as-is: OpenAB
    # itself expands those natively from its own process environment at
    # startup, the same way config-kiro.toml/config-nvidia-lab.toml do in
    # the sibling discord-personal-assistant project. Only the Discord
    # token and the routing/allowlist shape below go through this script's
    # own fail-closed checks, since only those are this service's dedicated
    # security requirement.

    assert_effective_routing(rendered)
    return rendered


def assert_effective_routing(rendered_text: str) -> None:
    """Fail closed if the rendered config would let OpenAB connect with a
    routing/allowlist shape different from what this deployment is only
    ever supposed to have -- run before the token this text contains ever
    reaches the network, so a bad render never gets a single byte to Discord.
    """
    parsed = tomllib.loads(rendered_text)
    discord_cfg = parsed.get("discord", {})

    allowed_channels = set(discord_cfg.get("allowed_channels", []))
    if allowed_channels != set(EXPECTED_ALLOWED_CHANNELS):
        raise ConfigError(
            "effective allowed_channels must be exactly "
            f"{sorted(EXPECTED_ALLOWED_CHANNELS)}, got {sorted(allowed_channels)}"
        )

    if discord_cfg.get("allow_dm") is not False:
        raise ConfigError("allow_dm must be explicitly false -- this bridge never accepts DMs")
    if discord_cfg.get("allow_all_users") is True:
        raise ConfigError("allow_all_users must not be true -- use the concrete guild-member allowlist")

    allowed_users = discord_cfg.get("allowed_users", [])
    if not allowed_users:
        raise ConfigError(
            "allowed_users must be a non-empty allowlist before this service can start "
            "-- fill it in from the guild's actual member list (see openab/README.md); "
            "an empty list would let every member of the three search channels use the "
            "agent, which is not a deliberate least-privilege choice this template makes for you"
        )
    if not all(isinstance(u, str) and u.isdigit() for u in allowed_users):
        raise ConfigError("allowed_users must be a list of numeric Discord user ID strings")

    if not discord_cfg.get("bot_token"):
        raise ConfigError("rendered config has no bot_token")


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print("usage: runtime_config.py TEMPLATE OUTPUT [ALLOWED_USERS_JSON]", file=sys.stderr)
        return 2
    template_path, output_path = argv[:2]
    allowed_users_path = Path(argv[2]) if len(argv) == 3 else None

    template = Path(template_path).read_text(encoding="utf-8")
    try:
        allowed_users = (
            json.loads(allowed_users_path.read_text(encoding="utf-8"))
            if allowed_users_path
            else None
        )
        rendered = render_config_text(
            template,
            token=os.environ.get(TOKEN_ENV_VAR),
            allowed_users=allowed_users,
        )
    except ConfigError as exc:
        print(f"radar-agent config: {exc}", file=sys.stderr)
        return 1

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    tmp = output.with_name(output.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, output)
    os.chmod(output, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
