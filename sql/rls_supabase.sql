-- =============================================================================
-- OPTIONAL: Row-Level Security (multi-tenant isolation by project membership)
-- Rule: security-rls-basics (CRITICAL) + security-rls-performance
--
-- Apply this ONLY if you move to Supabase (or otherwise want DB-enforced tenant
-- isolation). It is intentionally kept separate from schema.sql because the
-- classic self-hosted Flask deployment connects with a single trusted role and
-- enforces access in the service layer; enabling FORCE RLS there would block
-- every query unless the request context GUC below is set on each connection.
--
-- Two context styles are provided:
--   (A) Supabase Auth  -> policies use auth.uid() (a uuid). Requires lm_users.id
--                         to map to auth.users; adjust the membership join.
--   (B) Custom GUC     -> the app runs `SET app.current_user_id = '<id>'` at the
--                         start of each request/transaction. Shown below as the
--                         portable default (works on any Postgres).
--
-- Wrap the current user lookup in a SECURITY DEFINER helper so the membership
-- check is indexed and evaluated once (security-rls-performance).
-- =============================================================================

begin;

-- Portable "current user id" from a session GUC (style B). Returns NULL when
-- unset, which -- combined with the policies below -- denies all rows.
create or replace function app_current_user_id() returns bigint
language sql stable as $$
  select nullif(current_setting('app.current_user_id', true), '')::bigint;
$$;

-- Is the current user a member of the given project? Wrapped + STABLE so the
-- planner caches it per statement instead of re-running per row.
create or replace function app_is_project_member(pid bigint) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from lm_project_members m
    where m.project_id = pid
      and m.user_id = app_current_user_id()
  );
$$;

create or replace function app_is_system_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce(
    (select u.is_system_admin from lm_users u where u.id = app_current_user_id()),
    false
  );
$$;

-- --- Project-scoped tables: visible/writable only to members (or a sysadmin) ---
do $$
declare t text;
begin
  foreach t in array array[
    'lm_projects', 'lm_project_members', 'lm_field_definitions',
    'lm_project_models', 'lm_test_items', 'lm_cell_comments',
    'lm_data_jobs', 'lm_collab_doc', 'lm_collab_presence'
  ]
  loop
    execute format('alter table %I enable row level security;', t);
    execute format('alter table %I force  row level security;', t);
  end loop;
end $$;

-- lm_projects keys on its own id; the rest key on project_id.
create policy p_projects on lm_projects
  for all using (app_is_system_admin() or app_is_project_member(id))
  with check (app_is_system_admin() or app_is_project_member(id));

do $$
declare t text;
begin
  foreach t in array array[
    'lm_project_members', 'lm_field_definitions', 'lm_project_models',
    'lm_test_items', 'lm_cell_comments', 'lm_data_jobs',
    'lm_collab_doc', 'lm_collab_presence'
  ]
  loop
    execute format($f$
      create policy %1$s_member on %1$I
        for all using (app_is_system_admin() or app_is_project_member(project_id))
        with check (app_is_system_admin() or app_is_project_member(project_id));
    $f$, t);
  end loop;
end $$;

-- --- Global tables ---
-- lm_users: a user sees themselves; sysadmin sees all. Tune for your auth flow.
alter table lm_users enable row level security;
alter table lm_users force  row level security;
create policy p_users on lm_users
  for all using (app_is_system_admin() or id = app_current_user_id())
  with check (app_is_system_admin() or id = app_current_user_id());

-- tasks / task_events are project-scoped but project_id is nullable (legacy
-- unscoped rows). Members see their project's tasks; sysadmin sees all.
alter table tasks enable row level security;
alter table tasks force  row level security;
create policy p_tasks on tasks
  for all using (
    app_is_system_admin()
    or (project_id is not null and app_is_project_member(project_id))
    or submitter_id = app_current_user_id()
  )
  with check (
    app_is_system_admin()
    or (project_id is not null and app_is_project_member(project_id))
    or submitter_id = app_current_user_id()
  );

alter table task_events enable row level security;
alter table task_events force  row level security;
create policy p_task_events on task_events
  for all using (
    app_is_system_admin()
    or exists (
      select 1 from tasks tk
      where tk.id = task_events.task_id
        and (
          (tk.project_id is not null and app_is_project_member(tk.project_id))
          or tk.submitter_id = app_current_user_id()
        )
    )
  );

-- app_settings and lm_audit_logs are admin-only surfaces.
alter table app_settings enable row level security;
alter table app_settings force  row level security;
create policy p_settings on app_settings
  for all using (app_is_system_admin()) with check (app_is_system_admin());

alter table lm_audit_logs enable row level security;
alter table lm_audit_logs force  row level security;
create policy p_audit_read on lm_audit_logs
  for select using (
    app_is_system_admin()
    or (project_id is not null and app_is_project_member(project_id))
  );
-- Writes to the audit log should go through a trusted/service role that bypasses
-- RLS (e.g. a BYPASSRLS role), so no INSERT policy is granted here.

commit;

-- Supporting indexes for the membership check (already present in schema.sql:
-- uq_member_project_user covers (project_id, user_id); lm_project_members_user_id_idx
-- covers the reverse lookup used by app_is_project_member()).
