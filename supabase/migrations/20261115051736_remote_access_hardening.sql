-- Application clients have no direct database privileges. Explicit deny policies
-- also protect private tables if a future accidental grant exposes a relation.
do $$ declare relation record; begin
  for relation in select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
    where n.nspname='jobflow_private' and c.relkind='r'
  loop
    execute format('alter table jobflow_private.%I enable row level security', relation.relname);
    execute format('revoke all on table jobflow_private.%I from public, anon, authenticated', relation.relname);
    if not exists(select 1 from pg_policies where schemaname='jobflow_private'
                  and tablename=relation.relname and policyname='deny_client_access') then
      execute format('create policy deny_client_access on jobflow_private.%I for all to anon, authenticated using (false) with check (false)', relation.relname);
    end if;
  end loop;
  -- Supabase's optional auto-RLS event trigger needs no client EXECUTE grant.
  if to_regprocedure('public.rls_auto_enable()') is not null then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end $$;
