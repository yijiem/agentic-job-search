-- Private file manifests. Object bytes live in a non-public Storage bucket.
create table if not exists jobflow_private.files (
  workspace_id uuid not null references jobflow_private.workspaces(id) on delete cascade,
  path text not null,
  object_key text not null,
  sha256 text not null check (sha256 ~ '^[a-f0-9]{64}$'),
  size bigint not null check (size between 0 and 536870912),
  stored_size bigint not null check (stored_size between 0 and 536870912),
  encoding text not null check (encoding in ('identity', 'gzip')),
  updated_at timestamptz not null default now(),
  primary key (workspace_id, path)
);
alter table jobflow_private.files enable row level security;
revoke all on jobflow_private.files from public, anon, authenticated;
grant select, insert, update, delete on jobflow_private.files to service_role;

create or replace function public.jobflow_file_get(p_workspace uuid, p_path text)
returns jsonb language sql stable security invoker set search_path = '' as $$
  select to_jsonb(f) - 'workspace_id'
  from jobflow_private.files f where workspace_id=p_workspace and path=p_path;
$$;

create or replace function public.jobflow_file_list(p_workspace uuid, p_prefix text default '', p_after text default '', p_limit integer default 500)
returns jsonb language sql stable security invoker set search_path = '' as $$
  select coalesce(jsonb_agg(to_jsonb(f) - 'workspace_id' order by f.path), '[]'::jsonb)
  from (select * from jobflow_private.files where workspace_id=p_workspace
        and starts_with(path,p_prefix) and path>p_after
        order by path limit least(greatest(p_limit,1),1000)) f;
$$;

create or replace function public.jobflow_file_put(p_workspace uuid, p_token text, p_file jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare result jsonb; p text := p_file->>'path';
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if p is null or length(p)>2000 or p ~ '(^/|\\|[[:cntrl:]]|(^|/)\.{1,2}(/|$)|//|(^|/)\.)'
     or not (p in ('config/settings.json','config/profile.json','config/onboarding.json','config/hosted.json','data/cloud-migration.json')
             or starts_with(p,'inputs/') or starts_with(p,'reports/'))
     or starts_with(p,'inputs/web/') then
    raise exception 'Invalid private file path';
  end if;
  if (p_file->>'encoding') not in ('identity','gzip') or
     (p_file->>'object_key') is distinct from (p_workspace::text || '/' || (p_file->>'sha256') ||
       (case when p_file->>'encoding'='gzip' then '.gz' else '' end)) then
    raise exception 'Invalid private object reference';
  end if;
  insert into jobflow_private.files(workspace_id,path,object_key,sha256,size,stored_size,encoding)
  values(p_workspace,p,p_file->>'object_key',p_file->>'sha256',(p_file->>'size')::bigint,
         (p_file->>'stored_size')::bigint,p_file->>'encoding')
  on conflict(workspace_id,path) do update set object_key=excluded.object_key,
    sha256=excluded.sha256,size=excluded.size,stored_size=excluded.stored_size,
    encoding=excluded.encoding,updated_at=now();
  select public.jobflow_file_get(p_workspace,p) into result;
  return result;
end;
$$;

revoke all on function public.jobflow_file_get(uuid,text) from public, anon, authenticated;
revoke all on function public.jobflow_file_list(uuid,text,text,integer) from public, anon, authenticated;
revoke all on function public.jobflow_file_put(uuid,text,jsonb) from public, anon, authenticated;
grant execute on function public.jobflow_file_get(uuid,text) to service_role;
grant execute on function public.jobflow_file_list(uuid,text,text,integer) to service_role;
grant execute on function public.jobflow_file_put(uuid,text,jsonb) to service_role;

-- Provision the private bucket through Storage's HTTP API, not by modifying
-- Storage-owned tables. No browser-accessible object policies are granted.
