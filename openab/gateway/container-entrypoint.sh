#!/bin/bash
set -eu

# GATEWAY container entrypoint. Runs as the container's single fixed
# unprivileged user throughout -- no root anywhere (Dockerfile's final
# USER, Compose's `user:`, `cap_drop: [ALL]`, `no-new-privileges`, and a
# read-only root filesystem together rule it out). PID 1 (this script,
# then `openab`) spawns bridge-client.mjs as its `[agent] command` -- a
# pure byte relay to the codex-acp SIDECAR container (see
# openab/sidecar/), never codex-acp itself, which never runs in this
# container at all and therefore never shares a UID or filesystem with
# whatever OpenAB does. This container never holds a database credential
# or Codex auth in any form. Since PID 1 and bridge-client.mjs still share
# this container's one UID, file permissions alone cannot keep it from
# reading the rendered runtime config below if it were still on disk when
# a session starts -- so instead of relying on ownership, this deletes
# that file immediately after starting `openab`, before the Discord
# adapter can possibly accept a message (and therefore before any
# bridge-client.mjs child can possibly be spawned). `openab` reads the
# whole config into memory once at startup and never re-opens the path
# afterward -- verified in openab/README.md's documented offline checks
# by unlinking the file this way and confirming `openab` still logs
# "config loaded" / "discord bot running" and answers control-socket
# queries normally afterward.
#
# The Discord token itself is read from a Docker *secret* (a file under
# /run/secrets/, see docker-compose.yml), never from an environment
# variable -- a container's `environment`/`env_file` values become part
# of PID 1's own /proc/1/environ, which is an immutable snapshot of its
# exec-time environment: nothing this script does afterward (`unset`
# included) can retroactively remove a value that was already there when
# PID 1 started (verified live: `unset` on a running process correctly
# keeps a *freshly forked child* from inheriting a variable, but never
# changes what /proc/1/environ itself already reports). Reading the
# secret from a file and passing it to runtime_config.py as a
# command-scoped variable (`VAR=value command`, never `export`) means
# this shell's own environment table -- and therefore PID 1's
# /proc/1/environ -- never contains the token at any point.

umask 077
runtime_dir=/run/radar-agent
runtime_config="$runtime_dir/openab.toml"
token_secret=/run/secrets/openab_discord_bot_token

mkdir -p "$runtime_dir"
chmod 0700 "$runtime_dir"

if [ ! -r "$token_secret" ]; then
  echo "container-entrypoint: $token_secret is not present -- bootstrap the openab_discord_bot_token Docker secret (see openab/README.md) before starting this service" >&2
  exit 1
fi

# Command-scoped assignment (`VAR=value command`), not `export`: the
# token exists in the environment of this ONE exec()'d subprocess
# (runtime_config.py, a short-lived helper that reads it once and writes
# it into the rendered file) and nowhere else -- this shell's own
# variable table is never touched, so /proc/1/environ for this script
# (and therefore for `openab`, forked from it below) never contains it.
OPENAB_DISCORD_BOT_TOKEN="$(cat "$token_secret")" \
  python3 /opt/radar-agent/runtime_config.py /etc/openab/config.toml "$runtime_config"

openab run -c "$runtime_config" &
openab_pid=$!

# openab parses the whole config synchronously as the very first thing it
# does at startup (its own "config loaded" log line is always the first
# output, before it touches its cache/reminders files or opens its control
# socket) -- a short bounded wait is enough margin before this deletes the
# only on-disk copy of the token. If openab has already exited (a config
# it rejects outright), don't mask that failure by looping further; `wait`
# below reports the real exit status either way.
sleep 1
rm -f "$runtime_config"

trap 'kill -TERM "$openab_pid" 2>/dev/null || true' HUP INT TERM
wait "$openab_pid"
exit $?
