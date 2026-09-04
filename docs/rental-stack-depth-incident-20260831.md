# Rental crawler incident — 2026-08-31

## Symptom

PostgreSQL raised `psycopg.errors.StatementTooComplex` / `max_stack_depth` while loading existing rental properties. The generated tuple `IN` predicate contained thousands of `(source, source_property_id)` pairs in one statement.

## Fix

Rental preload queries are now chunked into batches of 500 keys in both the pipeline and repository bulk-upsert readback path. This preserves the preload/bulk-upsert behavior while preventing an excessively deep PostgreSQL expression tree.

## Verification

`tests/test_rental_status_lifecycle.py`, `tests/test_crawler_and_pipeline.py`, and `tests/test_repository_and_commands.py`: all passed.

The next scheduled rental run should be observed for batch count, runtime, and any remaining database errors before further tuning.
