-- =============================================================================
-- Undo rls_supabase.sql: turn OFF row-level security and drop its policies.
--
-- Use this when the classic self-hosted Flask deployment connects with a single
-- trusted role and enforces access in the service layer. In that model FORCE RLS
-- blocks legitimate writes (e.g. the startup license/admin seed into
-- app_settings, which runs with no logged-in user), producing:
--     new row violates row-level security policy for table "app_settings"
--
-- Running this makes every table behave normally again. It is idempotent and
-- safe to run whether or not rls_supabase.sql was applied. It does NOT touch any
-- data -- only the RLS flags, policies, and (optionally) the helper functions.
-- =============================================================================

begin;
set client_min_messages = warning;

-- 1) Disable + un-force RLS on every table (order/existence-independent).
do $$
declare t text;
begin
  foreach t in array array[
    'app_settings', 'lm_users', 'lm_projects', 'lm_project_members',
    'lm_field_definitions', 'lm_project_models', 'lm_test_items',
    'lm_cell_comments', 'lm_audit_logs', 'lm_data_jobs',
    'lm_collab_doc', 'lm_collab_presence', 'tasks', 'task_events'
  ]
  loop
    if to_regclass(format('public.%I', t)) is not null then
      execute format('alter table public.%I no force row level security;', t);
      execute format('alter table public.%I disable row level security;', t);
    end if;
  end loop;
end $$;

-- 2) Drop every policy created by rls_supabase.sql (harmless once RLS is off,
--    but removed so a later re-apply of rls_supabase.sql starts clean).
drop policy if exists p_settings   on app_settings;
drop policy if exists p_users      on lm_users;
drop policy if exists p_projects   on lm_projects;
drop policy if exists p_tasks      on tasks;
drop policy if exists p_task_events on task_events;
drop policy if exists p_audit_read on lm_audit_logs;

-- Per-table "<table>_member" policies from the project-scoped loop.
drop policy if exists lm_project_members_member    on lm_project_members;
drop policy if exists lm_field_definitions_member  on lm_field_definitions;
drop policy if exists lm_project_models_member     on lm_project_models;
drop policy if exists lm_test_items_member         on lm_test_items;
drop policy if exists lm_cell_comments_member      on lm_cell_comments;
drop policy if exists lm_data_jobs_member          on lm_data_jobs;
drop policy if exists lm_collab_doc_member         on lm_collab_doc;
drop policy if exists lm_collab_presence_member    on lm_collab_presence;

-- 3) (Optional) drop the helper functions. Commented out by default so you can
--    re-apply rls_supabase.sql later without recreating them. Uncomment to fully
--    remove every trace of the RLS layer.
-- drop function if exists app_is_project_member(bigint);
-- drop function if exists app_is_system_admin();
-- drop function if exists app_current_user_id();

commit;

-- Verify (should list no rows once this has run):
--   select relname from pg_class
--   where relrowsecurity and relnamespace = 'public'::regnamespace;
