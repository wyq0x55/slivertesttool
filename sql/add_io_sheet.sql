-- Add the 入出力 (io) sheet to the sheet CHECK constraints.
--
-- The original schema restricts lm_field_definitions.sheet and
-- lm_test_items.sheet to ('test','const','lib') via an INLINE column check,
-- which PostgreSQL auto-names "<table>_sheet_check". This migration widens both
-- to allow the new 'io' reference pool WITHOUT touching data. Idempotent and
-- safe to re-run: it drops whichever variant of the constraint exists (the
-- auto-named inline one, or a named one from the SQLAlchemy models) and adds a
-- single named constraint that includes 'io'.
--
-- Run against the existing database, e.g.:
--   psql "$DATABASE_URL" -f sql/add_io_sheet.sql

begin;

-- lm_field_definitions.sheet
alter table lm_field_definitions
  drop constraint if exists lm_field_definitions_sheet_check;
alter table lm_field_definitions
  drop constraint if exists ck_field_sheet;
alter table lm_field_definitions
  add constraint ck_field_sheet
  check (sheet in ('test','const','lib','io'));

-- lm_test_items.sheet
alter table lm_test_items
  drop constraint if exists lm_test_items_sheet_check;
alter table lm_test_items
  drop constraint if exists ck_item_sheet;
alter table lm_test_items
  add constraint ck_item_sheet
  check (sheet in ('test','const','lib','io'));

commit;
