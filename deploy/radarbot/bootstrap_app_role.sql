-- Dedicated least-privilege application role for the long-running
-- deterministic RadarBot. The role may read and mutate existing Radar
-- tables and sequences for slash-command subscriptions, but cannot
-- create schemas/databases/tables or use TEMP. Migrations remain the
-- responsibility of the separate operator-controlled Alembic workflow.

\if :{?app_password}
\else
  \set ON_ERROR_STOP on
  SELECT 1/0 AS app_password_variable_was_not_set;
\endif

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_bot_app') THEN
    CREATE ROLE radar_bot_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
  END IF;
END
$$;

ALTER ROLE radar_bot_app WITH PASSWORD :'app_password';

GRANT CONNECT ON DATABASE radar TO radar_bot_app;
REVOKE TEMP, CREATE ON DATABASE radar FROM radar_bot_app;
GRANT USAGE ON SCHEMA public TO radar_bot_app;
REVOKE CREATE ON SCHEMA public FROM radar_bot_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO radar_bot_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO radar_bot_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO radar_bot_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO radar_bot_app;
