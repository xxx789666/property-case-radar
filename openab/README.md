# openab — Property Case Radar Discord LLM bridge

Provides the active natural-language Q&A entry point over Property Case
Radar's data, backed by
[OpenAB](https://github.com/openabdev/openab) 0.10.0-beta.2 (the `-codex`
image variant) driving [Codex ACP](https://github.com/openai/codex) as the
LLM. The deployed Windows configuration replaces the deterministic
`/house` and `/auction` Discord entry point: those commands are unregistered
and `apps.discord_bot` is not scheduled. Mentioning `@Property Case Radar`
in an allowlisted search channel creates a thread; follow-up messages in that
thread do not require another mention. The Windows configuration accepts both
the bot user mention (`1530136356439719997`) and the same-named Property Case
Radar role mention (`1530137617952411779`) so either Discord autocomplete
selection reaches the Agent.

The current host uses the native Windows release because Docker is not
installed. The hardened Docker Compose topology below remains the portable
deployment alternative and the source of the gateway/sidecar separation
design.

## Native Windows deployment (current host)

```powershell
python scripts/bootstrap_openab_windows.py
powershell -ExecutionPolicy Bypass -File scripts/install_openab_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts/register_openab_tasks.ps1
```

- OpenAB and npm runtime artifacts: `openab/.runtime/` (gitignored)
- Secrets: `openab/.local/` (gitignored and restricted to the current user)
- Gateway log: `logs/openab-gateway.log`
- Sidecar log: `logs/openab-sidecar.log`
- Per-query JSONL audit: `logs/openab-query.jsonl`
- Windows tasks: `Property Case Radar OpenAB Gateway` and
  `Property Case Radar OpenAB Sidecar` (at user logon, restart on failure),
  plus `Property Case Radar Discord Q&A Allowlist Sync` (every 5 minutes)
- Sidecar listener: loopback only, `127.0.0.1:18765`

The native template is `openab/windows/config-radar-agent.toml`. The gateway
holds the Discord token but never `DATABASE_URL`; the sidecar reads the
dedicated `radar_agent_ro` URL and passes it only to each ACP child.
The allowlist sync writes only verified members who already hold the
`Radar 問答` role to the gitignored runtime allowlist, and restarts the
gateway only when role membership changes. It never grants the verification
role itself. OpenAB's
`allowed_role_ids` setting is intentionally not used for authorization:
upstream defines it as a role-mention trigger, while `allowed_users` is the
actual identity gate.

The implementation was verified offline (see "Offline verification" below),
including actually building both pinned images, running fully-hardened
two-container end-to-end tests, and testing against a real,
automatically-provisioned, disposable PostgreSQL instance (never a live or
shared instance, never real credentials). A bounded live Discord smoke on
2026-07-24 subsequently confirmed that the dedicated bot can log in and
that the `/house` and `/auction` command groups are synchronized; the final
in-channel reply remains blocked because channel `1530076529751756870`
lacks `Send Messages` for the bot. No Discord permission was changed and no
real token or credential was written to this repository.

## Security review history

This design has gone through three rounds of security review; every raised
finding was fixed and re-verified for real (not just re-documented):

**Round 1**: non-root container, session-level DB read-only, `.dockerignore`
+ APT drift, mandatory `DATABASE_URL`, token-naming clarity, targeted tests.

**Round 2**: found and fixed a real gap -- `unset` cannot retroactively clear
a variable from `/proc/1/environ` (an immutable exec-time snapshot); moved
both secrets to Docker Compose file-based secrets. Hardened the DB
boundary to a real PostgreSQL catalog privilege check (not just a session
setting) with a dedicated read-only role, proven against a real disposable
Postgres including a direct-bypass connection and a `SET TRANSACTION READ
WRITE` override, both still rejected. Removed a second live APT source
(`github-cli.list`).

**Round 3** (this revision) -- **split OpenAB and Codex ACP into two
separate containers**:

1. **Two containers, two UIDs' worth of isolation, no shared secret
   mounts**: the gateway (OpenAB) holds only the Discord token; the
   sidecar (codex-acp) holds only Codex auth + the read-only DB secret.
   Neither container has a Docker socket, host PID namespace, or any
   mount the other also has. See "Architecture" below.
2. **No official remote-agent transport exists for this** -- confirmed
   against OpenAB's own README (its `[agent]` config only ever spawns a
   local subprocess; the only built-in remote option is an AWS Bedrock
   AgentCore-specific bridge, not applicable here) and, for wire format,
   directly against the ACP TypeScript SDK's `dist/line-buffer.js` (which
   codex-acp is built on): ACP-over-stdio is newline-delimited JSON. Built
   a minimal custom stdio<->TCP bridge
   (`openab/gateway/bridge-client.mjs` + `openab/sidecar/bridge-server.mjs`)
   instead: a pure byte relay with no shell, no arbitrary exec, framing
   preserved by construction (raw `pipe()`, never re-parsed), backpressure
   via Node's own flow control, and a bounded-timeout shutdown path for
   every disconnect scenario. See "Bridge" below.
3. **PostgreSQL role hardened further**: revokes `TEMP` (granted to
   `PUBLIC` on every database by default -- closes `CREATE TEMP TABLE`)
   and sequence `USAGE` (not just `UPDATE` -- closes `nextval()`/
   `currval()`, not only `setval()`). Verified live against a real
   disposable Postgres: permanent DDL/DML, `CREATE TEMP TABLE`, and
   `nextval()` are all rejected even after explicitly requesting
   `SET TRANSACTION READ WRITE`, and database state is unchanged
   afterward.
4. **Safe bootstrap password flow**: `bootstrap_read_only_role.sql` now
   takes the password as a psql variable (`-v ro_password=...`, substituted
   via `:'ro_password'`), never a hardcoded placeholder a human might
   deploy verbatim.
5. Stale single-container docs/paths removed throughout.

## Architecture

```text
Discord #房地案件-搜尋 / #法拍案件-搜尋 (private, allowlisted)
  │  @dedicated bot, natural-language question
  ▼
┌─────────────────────────────┐        radar-internal-bridge        ┌──────────────────────────────┐
│ openab-gateway container    │◄────────(internal:true network,────►│ openab-sidecar container     │
│ (non-root, cap_drop:[ALL])  │         no other members,           │ (non-root, cap_drop:[ALL])   │
│                              │         no external route)          │                               │
│ container-entrypoint.sh     │                                      │ bridge-server.mjs (PID 1)     │
│  reads openab_discord_      │                                      │  reads radar_agent_database_  │
│  bot_token Docker secret,   │                                      │  url Docker secret, never its │
│  renders OpenAB's config,   │                                      │  own env; per connection:     │
│  never its own env either   │                                      │  spawns codex-acp with        │
│  (see "Token boundary")     │                                      │  DATABASE_URL in *that        │
│         │                   │                                      │  child's* env only            │
│         ▼                   │                                      │         │                     │
│ openab (Discord gateway)    │                                      │         ▼                     │
│  [agent] command =          │                                      │ codex-acp -- ACP session,     │
│  bridge-client.mjs ─────────┼──── ACP JSON-RPC bytes, relayed ─────┼─────────┼─ AGENTS.md context   │
│  (pure stdio<->TCP relay,   │      byte-for-byte, never parsed     │         ▼                     │
│  no shell, no exec)         │                                      │ tools/radar_agent_query.py    │
└─────────────────────────────┘                                      │  (subprocess) -- read-only,   │
         │                                                            │  DB-role-verified queries     │
         │ radar-gateway-egress                                       └──────────────────────────────┘
         ▼ (outbound only, Discord)                                    │ radar-db-net (internal:true)  │ radar-sidecar-egress
                                                                        ▼ (Codex model/auth API only)   ▼ (outbound only)
                                                                    ┌─────────────┐
                                                                    │ postgres    │  (never reachable
                                                                    └─────────────┘   from the gateway)
```

Four Compose networks give each container exactly the reachability it
needs and nothing else (see `docker-compose.yml`):

| Network | Members | `internal: true`? | Purpose |
|---|---|---|---|
| `radar-internal-bridge` | gateway, sidecar | Yes | ACP JSON-RPC relay only |
| `radar-db-net` | postgres, sidecar | Yes | Database traffic only |
| `radar-gateway-egress` | gateway | No | Outbound Discord connectivity |
| `radar-sidecar-egress` | sidecar | No | Outbound Codex model/auth API calls |

The gateway is **never** attached to `radar-db-net` -- it has no network
path to postgres at all, regardless of what credentials it might somehow
obtain. `internal: true` means Docker gives that network no route to
anything outside itself; only services explicitly attached to it can ever
reach each other over it.

## Bridge (gateway<->sidecar ACP relay)

OpenAB has no built-in way to run its `[agent]` as anything other than a
local subprocess (confirmed against the upstream README), so splitting
gateway and sidecar into separate containers required a small custom relay:

- `openab/gateway/bridge-client.mjs`: what OpenAB actually spawns as its
  `[agent] command`. Opens exactly one TCP connection to the sidecar and
  relays `process.stdin`/`process.stdout` to/from that socket via Node's
  own `stream.pipe()` -- raw bytes, never parsed as JSON or re-framed, so
  message boundaries (newline-delimited, per the ACP SDK) are preserved by
  construction and backpressure is handled by `pipe()`'s native flow
  control. No shell, no `child_process`, no filesystem write capability
  beyond its own stdio/socket -- a gateway process compromised via a
  malicious Discord message gets no capability beyond what a normal ACP
  stdio subprocess already has.
- `openab/sidecar/bridge-server.mjs`: listens on the internal bridge
  network only. On each connection, reads the `radar_agent_database_url`
  secret file directly (never assigning it to its own `process.env`),
  spawns the real `codex-acp` with that value in *its* env only, and
  relays the same way in reverse.
- Both sides implement a **bounded disconnect**: every terminal event
  (stdin end, socket error/close, child exit/error) drives a single
  shutdown path with a hard timeout (`ACP_BRIDGE_SHUTDOWN_TIMEOUT_MS`,
  default 5s) -- a wedged agent or a half-closed socket can never leave
  either process hanging indefinitely.
- `tests/test_openab_bridge.py` proves all of this against the real
  scripts over a real loopback TCP connection: request/response roundtrip,
  a message split across multiple small writes still arriving as one
  coherent line, a 2 MB single-line payload surviving intact, bounded
  failure when the sidecar is unreachable, and bounded shutdown when the
  agent process crashes mid-session.

## Token boundary — can this share RadarBot's token/process?

**No.** A single Discord bot token supports one healthy Gateway (websocket)
session at a time. Running two independent clients (`RadarBot`'s
`discord.py` process and OpenAB) that both `IDENTIFY` with the *same*
token is not a supported Discord configuration. `OPENAB_DISCORD_BOT_TOKEN`
(a Docker secret file) is wholly separate from RadarBot's `DISCORD_TOKEN`;
nothing in this directory reads RadarBot's token, and nothing in the main
app reads `OPENAB_DISCORD_BOT_TOKEN`. See "Bootstrap" below for both
application IDs.

If a dedicated second bot application is not available for some
deployment, the fallback is **not** to share a token -- it is to drop this
OpenAB layer entirely and keep only the deterministic `/house`/`/auction`
bot.

## Channels

Fixed to exactly the two **private** channels from `discord 伺服器.txt`
(same channels the deterministic bot already scopes `/house search` and
`/auction search` to):

| Channel | ID |
|---|---|
| 房地案件-搜尋 | `1530076451242508318` |
| 法拍案件-搜尋 | `1530076529751756870` |

`runtime_config.py` refuses to start OpenAB if the rendered config's
`allowed_channels` differs from this exact pair -- `allow_dm` must also be
`false`, and `allowed_users` must be non-empty.

## Least privilege

- **Two containers, no shared secret mounts**: the gateway never has a
  database credential or Codex auth in any form; the sidecar never has the
  Discord token in any form. Different UIDs are not needed for this
  guarantee -- the guarantee is that each *container* simply never
  receives the other's secret at all, verified by static Dockerfile
  checks (`tests/test_openab_two_container_split.py`) and a live
  offline run confirming neither secret appears in the other container's
  environment/files.
- **Process (no root anywhere, either container)**: each Dockerfile ends
  with a fixed unprivileged `USER`, and each Compose service additionally
  sets `user: "1000:1000"`, `cap_drop: ["ALL"]`,
  `security_opt: ["no-new-privileges:true"]`, and `read_only: true`, with
  `tmpfs` mounts (each explicitly `uid=1000,gid=1000`) for the paths each
  container writes to at runtime. Neither container mounts the Docker
  socket, shares the host PID namespace, or has any capability beyond
  what's listed here.
- **Secrets never become environment variables**: both the Discord token
  and the database connection string are Docker Compose file-based
  secrets, read directly from `/run/secrets/...` by the one process that
  needs each -- never inherited down a process chain as an
  `environment`/`env_file` value, because a container's env vars become
  part of PID 1's immutable `/proc/1/environ` (verified live in round 2).
- **Database (technically enforced read-only, two independent levels)**:
  `tools/radar_agent_query.py` never imports `database.repositories.*`,
  `crawlers.*`, or `apps.config` (not present in the sidecar image at
  all), forces every PostgreSQL connection into
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (Level 1,
  defense in depth), and independently queries the catalog at every
  invocation to fail closed if `current_user` has *any* real write
  capability -- TEMP, CREATE (database or schema), any of
  INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on any table, or USAGE
  or UPDATE on any sequence (Level 2, survives a bypass of this tool's own
  code). Verified against a real disposable PostgreSQL server: the
  write-capable `radar` role fails this tool's own startup check outright;
  the dedicated `radar_agent_ro` role (see
  `openab/sidecar/bootstrap_read_only_role.sql`) passes it and serves real
  queries; and a direct bypass connection using that same read-only role
  -- including one that explicitly issues `SET TRANSACTION READ WRITE` --
  still gets `psycopg.errors.InsufficientPrivilege` on every write
  attempt (permanent INSERT/UPDATE/CREATE TABLE/DROP TABLE, `CREATE TEMP
  TABLE`, and `nextval()`), with database state unchanged afterward.
  `RADAR_AGENT_DATABASE_URL` is mandatory with no fallback anywhere in
  this path.
- **Container code surface**: the sidecar image copies in only
  `database/__init__.py`, `database/session.py`, `database/models/`,
  `notifications/auction_masking.py`, and `tools/radar_agent_query.py` --
  never `apps/`, `crawlers/`, `database/repositories/`, or
  `notifications/auction_notification.py` (imports `discord.py`). The
  gateway image copies in no Property Case Radar application code at all
  -- only `bridge-client.mjs` and `runtime_config.py`.
- **Masking**: `tools/radar_agent_query.py` always masks 債務人/所有權人
  and never surfaces `occupancy_note` at all -- more conservative than
  `/auction detail`'s own private-channel exemption, since this surface's
  request text ultimately comes from an LLM.

## Build reproducibility

- **`.dockerignore`** (repo root): excludes `.git/`, `.env`,
  `openab/.local/`, caches, and editor cruft from the build context.
- **Only the frozen Debian snapshot as an APT source, in both images**:
  each Dockerfile removes `/etc/apt/sources.list.d/github-cli.list` (a
  live, non-snapshotted `cli.github.com` source neither container has any
  use for) and points the remaining `debian.sources` at
  `snapshot.debian.org`'s frozen archive for the exact timestamp the base
  image's own file already recorded. Verified with fresh
  `docker build --no-cache` for both images.

## Secrets

Docker Compose file-based secrets, not `environment`/`env_file`. Neither
secret file exists in this repo.

| File | Contents | Committed? |
|---|---|---|
| `openab/openab_discord_bot_token.example` | Placeholder text | Yes |
| `openab/radar_agent_database_url.example` | Placeholder connection string | Yes |
| `openab/.local/openab_discord_bot_token` | The real OpenAB bot token, one line | **No** (gitignored) |
| `openab/.local/radar_agent_database_url` | The real, read-only-role connection string, one line | **No** (gitignored) |

```powershell
New-Item -ItemType Directory -Force openab/.local | Out-Null
Copy-Item openab/openab_discord_bot_token.example openab/.local/openab_discord_bot_token
Copy-Item openab/radar_agent_database_url.example openab/.local/radar_agent_database_url
# Edit these two files directly with a real editor -- never paste a real
# token or connection string into a shell command, PowerShell history, an
# issue, or a chat message.
```

## Version pins

| Component | Pin |
|---|---|
| OpenAB image (both containers) | `ghcr.io/openabdev/openab@sha256:6e9c43e7acfa02f3886b22a209f1140842fc01807927b77a1f55cb0644393b9c` (resolved from the `0.10.0-beta.2-codex` tag on 2026-07-24; pinned by immutable digest) |
| `openab` binary | `0.10.0` |
| `codex-acp` (`@agentclientprotocol/codex-acp`) | `1.1.4` |
| Codex CLI | `0.144.1` |
| `python3` / `python3-venv` (apt, Debian 13 "trixie", frozen snapshot) | `3.13.5-1` |
| Sidecar Python dependencies | hash-locked in `openab/sidecar/requirements.lock` |

## Bootstrap (live steps a human must do — not performed by this change)

Deployed application IDs (already created, per the deployment operator --
tokens are managed as coordinator/operator secrets and are never read or
handled by anything in this repository or its automation):

| Application | Discord application ID | Token |
|---|---|---|
| OpenAB (gateway) | `1530136356439719997` | `OPENAB_DISCORD_BOT_TOKEN` (Docker secret file) |
| RadarBot (`/house`, `/auction` -- existing, unrelated) | `1530138799290581153` | `DISCORD_TOKEN` (repo-root `.env`; see `apps/config.py`) |

1. In the Discord Developer Portal, create a **new, separate** application
   and bot (never reuse RadarBot's application). Minimum permissions: View
   Channels, Read Message History, Send Messages, Send Messages in
   Threads, Add Reactions.
2. Invite the bot to the guild (`1530072733818556538`) with access to only
   the two private search channels above.
3. Create the Discord token secret file (see "Secrets" above).
4. Create the dedicated read-only Postgres role and its secret file
   **without ever writing the password to a file or shell history**:

   ```powershell
   $RoPassword = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
   Get-Content -Raw openab/sidecar/bootstrap_read_only_role.sql | docker exec -i <postgres-container> `
     psql -v ON_ERROR_STOP=1 -U radar -d radar -v ro_password="$RoPassword"
   Set-Content -NoNewline openab/.local/radar_agent_database_url `
     "postgresql+psycopg://radar_agent_ro:$RoPassword@postgres:5432/radar"
   Remove-Variable RoPassword
   ```

   (PowerShell has no `<` stdin-redirection operator for external commands
   the way POSIX shells do -- `Get-Content -Raw ... | docker exec -i ...`
   is the actually-executable equivalent: `-Raw` reads the file as one
   string, preserving it exactly, and the pipeline feeds it to `docker
   exec`'s stdin.)

   (`bootstrap_read_only_role.sql` refuses to run at all -- via its own
   `\if :{?ro_password}` check -- if `-v ro_password=...` is omitted, so
   there is no way to accidentally apply it without a real password.)
5. Confirm `openab/gateway/config-radar-agent.toml`'s `allowed_users`
   remains the bounded-REST-verified guild allowlist. `runtime_config.py`
   refuses to start the gateway if this list is empty or malformed.
6. `docker compose --profile openab build openab-gateway openab-sidecar`,
   then one-time Codex ChatGPT/API auth in its own dedicated volume:

   ```powershell
   docker compose --profile openab run --rm --no-deps `
     openab-sidecar sh -c "codex login --device-auth"
   ```

7. `docker compose --profile openab up -d openab-sidecar openab-gateway`,
   then `docker logs openab-gateway --tail 50` and confirm
   `allow_all_channels=false`, `channels=2`, `allow_dm=false`, and no
   token in the log; `docker logs openab-sidecar --tail 50` and confirm no
   database credential in the log.
8. Only after the above: the live smoke test below.

## Offline verification (what was actually run to build this)

```powershell
# Compose structure (neither secret file needs to exist yet)
docker compose --profile openab config --no-env-resolution --quiet

# Build both pinned images from scratch, no layer cache
docker compose --profile openab build --no-cache openab-gateway openab-sidecar

# Bridge: framing, backpressure, bounded disconnect -- real Node scripts,
# real loopback TCP, no Docker required for this file
pytest tests/test_openab_bridge.py -q

# Read-only query tool + database boundary: SQLite (always) and a real,
# automatically-provisioned disposable PostgreSQL server (not skipped in
# any environment with Docker) -- write-capable role fails startup,
# dedicated read-only role passes and serves queries, direct bypass
# connections and SET TRANSACTION READ WRITE overrides still rejected by
# real PostgreSQL privileges (permanent DDL/DML, CREATE TEMP TABLE,
# nextval()), state unchanged
pytest tests/test_radar_agent_query.py -q

# Offline config/security static checks (both Dockerfiles non-root,
# reduced code surface, frozen-snapshot apt; compose network topology;
# secrets hygiene; import-surface allowlist)
pytest tests/test_openab_radar_agent_config.py -q

# Full two-container offline roundtrip + credential-noninterference probes
# (neither container's env/proc/files/network expose the other's secret)
pytest tests/test_openab_two_container_split.py -q
```

## Live smoke test

Observed on 2026-07-24: login succeeded and the Discord API reported the
expected synchronized command groups (`/house`: 6 subcommands;
`/auction`: 7 subcommands). Posting the smoke message to channel
`1530076529751756870` was rejected because the bot lacks `Send Messages`
there, so reply content, read-only behavior, and masking could not yet be
verified in-channel. This is an operator permission blocker; the test did
not change or work around Discord permissions.

1. Confirm bootstrap steps 1–7 above are complete.
2. From an allowlisted account, @-mention the bot in 法拍案件-搜尋 with a
   simple question ("最新的法拍案件有哪些？").
3. Confirm the reply arrives in-channel, is read-only in nature, and masks
   any 債務人/所有權人 it mentions.
4. From a non-allowlisted account (or a different channel), confirm no
   response.
5. `docker logs openab-gateway` / `docker logs openab-sidecar` --tail 100
   and confirm no token/credential, no unexpected-write stack trace.

## Ops

```powershell
docker compose --profile openab up -d openab-sidecar openab-gateway
docker compose --profile openab stop openab-gateway openab-sidecar
docker logs openab-gateway --tail 50
docker logs openab-sidecar --tail 50
docker compose --profile openab build openab-gateway openab-sidecar   # after any code/template change
```

## Troubleshooting

- `container-entrypoint: /run/secrets/openab_discord_bot_token is not
  present`: bootstrap step 3 wasn't completed.
- `bridge-server: cannot read /run/secrets/radar_agent_database_url`:
  bootstrap step 4 wasn't completed.
- `current_user has dangerous role attribute(s)` /
  `has database-level privilege(s)` / `has write privilege(s)` /
  `has sequence privilege(s)`: the configured
  `radar_agent_database_url` secret points at a role with real
  capability beyond SELECT -- re-run
  `openab/sidecar/bootstrap_read_only_role.sql` and point the secret at
  `radar_agent_ro`; do not work around this.
- Gateway logs `agent_spawn_error` / connection errors to the sidecar:
  confirm both are on `radar-internal-bridge` and `openab-sidecar` is
  actually running (`docker compose ps`).
- Bot appears in the guild but never replies: check
  `docker logs openab-gateway` for `starting discord adapter`; if
  `channels=0`, the rendered config didn't parse as expected.
