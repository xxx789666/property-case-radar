-- Dedicated read-only PostgreSQL role for the OpenAB / Codex ACP query
-- tool (tools/radar_agent_query.py). Run this once against the real
-- database after the application schema exists (alembic upgrade head),
-- then put this role's connection string in the radar_agent_database_url
-- Docker secret -- never the main app's write-capable role.
--
-- tools/radar_agent_query.py verifies, at every invocation, that
-- current_user has none of rolsuper/rolbypassrls/rolcreatedb/
-- rolcreaterole, no TEMP or CREATE privilege on the database/any schema,
-- no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on any table, and no
-- USAGE or UPDATE on any sequence -- refusing to run any query if any of
-- those checks fail. The grants below are exactly what that verification
-- expects to pass; do not add anything beyond SELECT/CONNECT to this role.
--
-- Idempotent: safe to re-run after adding new tables (a fresh migration)
-- to extend the SELECT grant to them, or to re-apply the REVOKEs if a
-- future PostgreSQL default ever changes.
--
-- Password handling: this script takes the password as a psql variable
-- (`ro_password`), substituted via `:'ro_password'` (psql's quoted-literal
-- substitution, not string concatenation) -- it is never a hardcoded
-- placeholder a human might accidentally deploy verbatim. Run it like:
--
--   read -r -s -p 'New radar_agent_ro password: ' RO_PASSWORD; echo
--   PGPASSWORD=... psql -v ON_ERROR_STOP=1 -U radar -d radar \
--     -v ro_password="$RO_PASSWORD" -f bootstrap_read_only_role.sql
--   unset RO_PASSWORD
--
-- (or generate one non-interactively: RO_PASSWORD="$(openssl rand -base64 24)").
-- See openab/README.md's "bootstrap" section for the full flow, including
-- writing the resulting connection string straight into the
-- radar_agent_database_url Docker secret file without ever printing it.

\if :{?ro_password}
\else
  \warn 'ro_password variable is not set -- pass -v ro_password="..." (see the header comment above); aborting.'
  -- psql's \quit has no exit-code argument (verified against psql 16 --
  -- \q/\quit always exits 0, which would let automation mistake this
  -- abort for a successful bootstrap). \set ON_ERROR_STOP on plus a
  -- statement that is guaranteed to fail (division by zero, independent
  -- of any table/role state) is what actually makes psql itself exit
  -- non-zero here, regardless of whether the caller remembered to pass
  -- -v ON_ERROR_STOP=1 on the command line.
  \set ON_ERROR_STOP on
  SELECT 1/0 AS ro_password_variable_was_not_set;
\endif

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_agent_ro') THEN
    CREATE ROLE radar_agent_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
  END IF;
END
$$;

ALTER ROLE radar_agent_ro WITH PASSWORD :'ro_password';

GRANT CONNECT ON DATABASE radar TO radar_agent_ro;
-- TEMP is granted to PUBLIC on every database by default (lets any role
-- CREATE TEMPORARY TABLE) -- explicitly revoked so this role has no
-- CREATE capability of any kind, not even a session-local one.
REVOKE TEMP ON DATABASE radar FROM PUBLIC;
REVOKE TEMP ON DATABASE radar FROM radar_agent_ro;
REVOKE CREATE ON DATABASE radar FROM radar_agent_ro;

GRANT USAGE ON SCHEMA public TO radar_agent_ro;
REVOKE CREATE ON SCHEMA public FROM radar_agent_ro;

-- SELECT only -- never INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO radar_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO radar_agent_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM radar_agent_ro;

-- No sequence privilege at all -- tools/radar_agent_query.py never reads
-- or advances a sequence, and USAGE alone would already permit
-- nextval()/currval(). Explicit REVOKE as defense in depth against any
-- future default grant (e.g. from a role membership change), not just
-- "don't GRANT it".
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM radar_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM radar_agent_ro;
