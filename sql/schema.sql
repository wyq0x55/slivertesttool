-- =============================================================================
-- Silver Test Tool / LAN Test Matrix -- PostgreSQL schema extras
--
-- Application tables are NOT defined here.
-- SQLAlchemy model metadata + app.bootstrap.reconcile_schema() are the single
-- application-schema authority for both fresh installs and upgrades.
--
-- This file only carries PostgreSQL-level objects that SQLAlchemy create_all()
-- does not express: extension setup, the shared updated_at trigger function,
-- table triggers, and comments.
-- =============================================================================

begin;

set client_min_messages = warning;

create extension if not exists pgcrypto;

create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

comment on table app_settings is
  'Runtime key/value settings shared by web + worker processes.';

drop trigger if exists trg_lm_users_updated on lm_users;
create trigger trg_lm_users_updated before update on lm_users
  for each row execute function set_updated_at();

drop trigger if exists trg_lm_projects_updated on lm_projects;
create trigger trg_lm_projects_updated before update on lm_projects
  for each row execute function set_updated_at();

drop trigger if exists trg_lm_field_definitions_updated on lm_field_definitions;
create trigger trg_lm_field_definitions_updated before update on lm_field_definitions
  for each row execute function set_updated_at();

drop trigger if exists trg_lm_test_items_updated on lm_test_items;
create trigger trg_lm_test_items_updated before update on lm_test_items
  for each row execute function set_updated_at();

drop trigger if exists trg_lm_collab_presence_updated on lm_collab_presence;
create trigger trg_lm_collab_presence_updated before update on lm_collab_presence
  for each row execute function set_updated_at();

commit;
