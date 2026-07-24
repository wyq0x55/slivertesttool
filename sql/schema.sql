-- =============================================================================
-- Silver Test Tool / LAN Test Matrix -- fresh PostgreSQL schema
-- Target: PostgreSQL 14+ (works on Supabase). Rebuild-from-scratch, no migration.
--
-- Design rules applied (Supabase Postgres best practices):
--   * schema-primary-keys   : bigint GENERATED ALWAYS AS IDENTITY (not serial)
--   * schema-data-types     : text over varchar(n); timestamptz over timestamp;
--                             native uuid; boolean; jsonb
--   * schema-constraints    : CHECK on closed enum sets; idempotent DO-blocks not
--                             needed here (fresh build) but constraints are named
--   * schema-foreign-key-indexes : every FK column is indexed
--   * query-composite-indexes    : multi-column indexes match query predicates
--   * query-partial-indexes      : partial indexes for `deleted_at IS NULL`
--   * advanced-jsonb-indexing    : GIN (jsonb_path_ops) on custom_values
--   * schema-lowercase-identifiers : all identifiers lowercase snake_case
--   * RLS is provided separately in rls_supabase.sql (opt-in).
--
-- NOTE: open/inconsistent vocabularies (test_items.result / priority /
--       workflow_status, field_definitions.data_type) are intentionally left as
--       unconstrained text -- the application treats them as open sets.
-- =============================================================================

begin;

set client_min_messages = warning;

-- Needed for gen_random_uuid() (bundled with PG13+; on Supabase already on).
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Shared trigger: keep updated_at current at the database level, so raw-SQL
-- writers, the CRDT materializer, and any tool that bypasses the ORM still
-- refresh the column (fixes app-only onupdate gaps).
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ===========================================================================
-- app_settings : cross-process key/value (license limit/in-use, model list)
-- ===========================================================================
create table app_settings (
  key   text primary key,
  value text not null default ''
);
comment on table app_settings is 'Runtime key/value settings shared by web + worker processes.';

-- ===========================================================================
-- lm_users
-- ===========================================================================
create table lm_users (
  id                   bigint generated always as identity primary key,
  username             text        not null unique,
  display_name         text        not null default '',
  password_hash        text        not null default '',
  email                text,
  status               text        not null default 'active'
                                    check (status in ('active','disabled')),
  is_system_admin      boolean     not null default false,
  must_change_password boolean     not null default false,
  failed_logins        integer     not null default 0,
  locked_until         timestamptz,
  last_login_at        timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
-- username UNIQUE already creates an index.
create index lm_users_status_idx on lm_users (status);
create trigger trg_lm_users_updated before update on lm_users
  for each row execute function set_updated_at();

-- ===========================================================================
-- lm_projects
-- ===========================================================================
create table lm_projects (
  id               bigint generated always as identity primary key,
  code             text        not null unique,
  name             text        not null,
  description      text        not null default '',
  status           text        not null default 'draft'
                                check (status in ('draft','active','frozen','archived')),
  owner_id         bigint      references lm_users(id) on delete set null,
  created_by       bigint      references lm_users(id) on delete set null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  deleted_at       timestamptz,
  tm_id_prefix     text,
  tm_summary_sheet text
);
create index lm_projects_owner_id_idx   on lm_projects (owner_id);
create index lm_projects_created_by_idx on lm_projects (created_by);
-- Listing live projects by status is the hot path -> partial composite index.
create index lm_projects_live_status_idx on lm_projects (status)
  where deleted_at is null;
create trigger trg_lm_projects_updated before update on lm_projects
  for each row execute function set_updated_at();

-- ===========================================================================
-- lm_project_members  (membership + role)
-- ===========================================================================
create table lm_project_members (
  id         bigint generated always as identity primary key,
  project_id bigint      not null references lm_projects(id) on delete cascade,
  user_id    bigint      not null references lm_users(id)    on delete cascade,
  role       text        not null default 'reader'
                          check (role in ('project_admin','editor','reviewer','reader')),
  created_at timestamptz not null default now(),
  constraint uq_member_project_user unique (project_id, user_id)
);
-- uq_member_project_user (project_id, user_id) already indexes project_id-leading
-- lookups; only the reverse direction (user's projects) needs its own index.
create index lm_project_members_user_id_idx on lm_project_members (user_id);

-- ===========================================================================
-- lm_field_definitions  (dynamic column metadata per project/sheet)
-- ===========================================================================
create table lm_field_definitions (
  id             bigint generated always as identity primary key,
  project_id     bigint      not null references lm_projects(id) on delete cascade,
  field_key      text        not null,
  display_name   text        not null default '',
  -- Open vocabulary (text/number/date/datetime/boolean/decimal/hex/select/...);
  -- left unconstrained on purpose.
  data_type      text        not null default 'text',
  sheet          text        not null default 'test'
                              check (sheet in ('test','const','lib')),
  is_system      boolean     not null default false,
  is_required    boolean     not null default false,
  is_readonly    boolean     not null default false,
  default_value  jsonb,
  validation_rule jsonb,
  option_source  jsonb,
  help_text      text        not null default '',
  display_order  integer     not null default 0,
  is_active      boolean     not null default true,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint uq_field_project_key unique (project_id, field_key)
);
-- Editor loads fields for a (project, sheet) ordered by display_order.
create index lm_field_definitions_project_sheet_idx
  on lm_field_definitions (project_id, sheet, display_order);
create trigger trg_lm_field_definitions_updated before update on lm_field_definitions
  for each row execute function set_updated_at();

-- ===========================================================================
-- lm_project_models  (per-project .sil plant models)
-- ===========================================================================
create table lm_project_models (
  id         bigint generated always as identity primary key,
  project_id bigint      not null references lm_projects(id) on delete cascade,
  name       text        not null,
  kind       text        not null default 'path'
                          check (kind in ('path','bundle')),
  sil_path   text        not null default '',
  bundle_dir text,
  created_by bigint      references lm_users(id) on delete set null,
  created_at timestamptz not null default now(),
  constraint uq_model_project_name unique (project_id, name)
);
create index lm_project_models_created_by_idx on lm_project_models (created_by);

-- ===========================================================================
-- lm_test_items  (the matrix rows; first-class core columns + JSONB overflow)
-- ===========================================================================
create table lm_test_items (
  id              bigint generated always as identity primary key,
  -- Native uuid: accepts both 32-char hex (server default) and 36-char dashed
  -- (client/CRDT minted). Permanently removes the old varchar length overflow.
  uuid            uuid        not null default gen_random_uuid(),
  project_id      bigint      not null references lm_projects(id) on delete cascade,
  row_order       integer     not null default 0,
  sheet           text        not null default 'test'
                              check (sheet in ('test','const','lib')),
  case_id         text        not null default '',
  title           text        not null default '',
  module          text,
  precondition    text        not null default '',
  test_steps      text        not null default '',
  expected_result text        not null default '',
  actual_result   text        not null default '',
  -- Open vocabularies -> unconstrained text (see README data-quality note).
  result          text        not null default 'Not Tested',
  priority        text,
  owner_id        bigint      references lm_users(id) on delete set null,
  tags            jsonb,
  comment         text        not null default '',
  custom_values   jsonb,
  workflow_status text        not null default 'Draft',
  version         integer     not null default 1,
  created_by      bigint      references lm_users(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_by      bigint      references lm_users(id) on delete set null,
  updated_at      timestamptz not null default now(),
  deleted_at      timestamptz,
  -- The CRDT layer upserts a row keyed by its own uuid within a project.
  constraint uq_item_project_uuid unique (project_id, uuid)
);
-- FK indexes.
create index lm_test_items_owner_id_idx   on lm_test_items (owner_id);
create index lm_test_items_created_by_idx on lm_test_items (created_by);
create index lm_test_items_updated_by_idx on lm_test_items (updated_by);
-- Hot query paths are always scoped to a project AND exclude soft-deleted rows.
create index lm_test_items_project_order_idx
  on lm_test_items (project_id, sheet, row_order) where deleted_at is null;
create index lm_test_items_project_case_idx
  on lm_test_items (project_id, case_id) where deleted_at is null;
create index lm_test_items_project_status_idx
  on lm_test_items (project_id, workflow_status) where deleted_at is null;
-- Containment search on dynamic custom fields (attributes @> '{...}').
create index lm_test_items_custom_values_gin
  on lm_test_items using gin (custom_values jsonb_path_ops);
create trigger trg_lm_test_items_updated before update on lm_test_items
  for each row execute function set_updated_at();

-- ===========================================================================
-- lm_cell_comments
-- ===========================================================================
create table lm_cell_comments (
  id           bigint generated always as identity primary key,
  project_id   bigint      not null references lm_projects(id)   on delete cascade,
  test_item_id bigint      not null references lm_test_items(id) on delete cascade,
  field_key    text        not null,
  content      text        not null default '',
  created_by   bigint      references lm_users(id) on delete set null,
  created_at   timestamptz not null default now(),
  edited_at    timestamptz,
  deleted_at   timestamptz
);
create index lm_cell_comments_project_id_idx on lm_cell_comments (project_id);
-- Comments are fetched per item, live ones only.
create index lm_cell_comments_item_idx on lm_cell_comments (test_item_id)
  where deleted_at is null;
create index lm_cell_comments_created_by_idx on lm_cell_comments (created_by);

-- ===========================================================================
-- lm_audit_logs  (append-only; intentionally NOT FK-linked to survive deletes)
-- ===========================================================================
create table lm_audit_logs (
  id            bigint generated always as identity primary key,
  request_id    text,
  batch_id      text,
  actor_id      bigint,
  action        text        not null,
  object_type   text        not null default '',
  object_id     text,
  project_id    bigint,
  old_value     jsonb,
  new_value     jsonb,
  client_ip     text,   -- plain text (app stores an already-formatted string)
  result        text        not null default 'success'
                            check (result in ('success','failure','error')),
  error_summary text,
  created_at    timestamptz not null default now()
);
create index lm_audit_logs_project_time_idx on lm_audit_logs (project_id, created_at desc);
create index lm_audit_logs_batch_idx        on lm_audit_logs (batch_id);
create index lm_audit_logs_created_at_idx   on lm_audit_logs (created_at desc);

-- ===========================================================================
-- lm_data_jobs  (import/export jobs)
-- ===========================================================================
create table lm_data_jobs (
  id                bigint generated always as identity primary key,
  project_id        bigint      not null references lm_projects(id) on delete cascade,
  job_type          text        not null check (job_type in ('import','export')),
  status            text        not null default 'pending'
                                check (status in ('pending','running','success','failed','cancelled')),
  original_filename text,
  stored_filename   text,
  parameters        jsonb,
  preview           jsonb,
  total_count       integer     not null default 0,
  success_count     integer     not null default 0,
  error_count       integer     not null default 0,
  result_file_path  text,
  created_by        bigint,
  created_at        timestamptz not null default now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  expires_at        timestamptz
);
create index lm_data_jobs_project_id_idx on lm_data_jobs (project_id);
-- Worker polls pending/running jobs; sweeper reaps expired ones.
create index lm_data_jobs_active_idx on lm_data_jobs (status, created_at)
  where status in ('pending','running');
create index lm_data_jobs_expires_idx on lm_data_jobs (expires_at)
  where expires_at is not null;

-- ===========================================================================
-- lm_collab_doc  (append-only Yjs/CRDT update log, one row per Y update)
-- ===========================================================================
create table lm_collab_doc (
  id            bigint generated always as identity primary key,
  project_id    bigint      not null references lm_projects(id) on delete cascade,
  seq           integer     not null,
  update        bytea       not null,
  doc_metadata  bytea,
  ts            double precision not null default 0.0,
  created_at    timestamptz not null default now(),
  constraint uq_collab_project_seq unique (project_id, seq)
);
-- uq_collab_project_seq already covers project_id-leading replay/compaction.

-- ===========================================================================
-- lm_collab_presence  (one heartbeat row per project; natural PK = project_id)
-- ===========================================================================
create table lm_collab_presence (
  project_id  bigint primary key references lm_projects(id) on delete cascade,
  connections integer     not null default 0,
  updated_at  timestamptz not null default now()
);
create trigger trg_lm_collab_presence_updated before update on lm_collab_presence
  for each row execute function set_updated_at();

-- ===========================================================================
-- tasks  (one queued/executed Silver test run)
-- ===========================================================================
create table tasks (
  id              bigint generated always as identity primary key,
  task_key        text        not null unique,           -- e.g. "T000001"
  task_name       text        not null default '',
  file_name       text        not null default '',
  submitter       text        not null default 'anonymous',
  project_id      bigint      references lm_projects(id) on delete set null,
  submitter_id    bigint      references lm_users(id)    on delete set null,
  test_id         text        not null default '',
  sil_relpath     text        not null default 'model.sil',
  sil_name        text        not null default '',
  status          text        not null default 'queued'
                              check (status in ('queued','running','passed','failed','cancelled')),
  progress        integer     not null default 0,
  result          text        not null default '',
  message         text        not null default '',
  cancel_requested boolean    not null default false,
  workspace       text        not null default '',
  report_path     text        not null default '',
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz
);
create index tasks_project_id_idx   on tasks (project_id);
create index tasks_submitter_id_idx on tasks (submitter_id);
-- Queue/worker scans un-finished tasks; dashboards list per-project by recency.
create index tasks_status_idx on tasks (status);
create index tasks_project_created_idx on tasks (project_id, created_at desc);

-- ===========================================================================
-- task_events  (SSE backbone; worker appends, web replays by id cursor)
-- ===========================================================================
create table task_events (
  id           bigint generated always as identity primary key,
  task_id      bigint      not null references tasks(id) on delete cascade,
  event_type   text        not null default 'log'
                            check (event_type in ('log','progress','warning','error','result','status')),
  message      text        not null default '',
  payload_json text        not null default '',
  created_at   timestamptz not null default now()
);
-- Replay cursor: events for a task ordered by id.
create index task_events_task_id_idx on task_events (task_id, id);

commit;
