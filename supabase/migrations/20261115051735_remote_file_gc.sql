-- Bound object storage costs while retaining every current manifest reference.
create index if not exists files_workspace_object on jobflow_private.files(workspace_id,object_key);
create table if not exists jobflow_private.file_garbage (
  workspace_id uuid not null references jobflow_private.workspaces(id) on delete cascade,
  object_key text not null,
  queued_at timestamptz not null default now(),
  deleting boolean not null default false,
  primary key(workspace_id,object_key)
);
alter table jobflow_private.file_garbage enable row level security;
revoke all on jobflow_private.file_garbage from public,anon,authenticated;
grant select,insert,update,delete on jobflow_private.file_garbage to service_role;

create or replace function public.jobflow_file_stage(p_workspace uuid,p_token text,p_object_key text)
returns void language plpgsql security invoker set search_path='' as $$
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if p_object_key !~ ('^' || p_workspace::text || '/[a-f0-9]{64}(\.gz)?$') then
    raise exception 'Invalid private object reference';
  end if;
  if exists(select 1 from jobflow_private.file_garbage where workspace_id=p_workspace
            and object_key=p_object_key and deleting) then
    raise exception 'Private object cleanup is in progress; retry the save';
  end if;
  insert into jobflow_private.file_garbage(workspace_id,object_key)
  values(p_workspace,p_object_key)
  on conflict(workspace_id,object_key) do update set queued_at=now();
end;
$$;

create or replace function public.jobflow_file_put(p_workspace uuid,p_token text,p_file jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare result jsonb; p text := p_file->>'path'; old_key text;
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
  if exists(select 1 from jobflow_private.file_garbage where workspace_id=p_workspace
            and object_key=p_file->>'object_key' and deleting) then
    raise exception 'Private object cleanup is in progress; retry the save';
  end if;
  select object_key into old_key from jobflow_private.files where workspace_id=p_workspace and path=p;
  insert into jobflow_private.files(workspace_id,path,object_key,sha256,size,stored_size,encoding)
  values(p_workspace,p,p_file->>'object_key',p_file->>'sha256',(p_file->>'size')::bigint,
         (p_file->>'stored_size')::bigint,p_file->>'encoding')
  on conflict(workspace_id,path) do update set object_key=excluded.object_key,
    sha256=excluded.sha256,size=excluded.size,stored_size=excluded.stored_size,
    encoding=excluded.encoding,updated_at=now();
  if old_key is not null and old_key<>p_file->>'object_key' then
    insert into jobflow_private.file_garbage(workspace_id,object_key) values(p_workspace,old_key)
    on conflict(workspace_id,object_key) do update set queued_at=now();
  end if;
  select public.jobflow_file_get(p_workspace,p) into result;
  return result;
end;
$$;

create or replace function public.jobflow_file_gc_claim(p_workspace uuid,p_token text,p_limit integer default 100)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare result jsonb;
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  with candidates as (
    select g.object_key from jobflow_private.file_garbage g
    where g.workspace_id=p_workspace and (g.deleting or g.queued_at<now()-interval '24 hours')
      and not exists(select 1 from jobflow_private.files f where f.workspace_id=p_workspace and f.object_key=g.object_key)
    order by g.queued_at,g.object_key limit least(greatest(p_limit,1),100)
  ), claimed as (
    update jobflow_private.file_garbage g set deleting=true from candidates c
    where g.workspace_id=p_workspace and g.object_key=c.object_key returning g.object_key
  ) select coalesce(jsonb_agg(object_key order by object_key),'[]'::jsonb) into result from claimed;
  return result;
end;
$$;

create or replace function public.jobflow_file_gc_finish(p_workspace uuid,p_token text,p_keys jsonb)
returns void language plpgsql security invoker set search_path='' as $$
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if jsonb_typeof(p_keys)<>'array' or jsonb_array_length(p_keys)>100 then
    raise exception 'Invalid private object cleanup batch';
  end if;
  delete from jobflow_private.file_garbage where workspace_id=p_workspace and deleting
    and object_key in (select jsonb_array_elements_text(p_keys));
end;
$$;

revoke all on function public.jobflow_file_stage(uuid,text,text) from public,anon,authenticated;
revoke all on function public.jobflow_file_put(uuid,text,jsonb) from public,anon,authenticated;
revoke all on function public.jobflow_file_gc_claim(uuid,text,integer) from public,anon,authenticated;
revoke all on function public.jobflow_file_gc_finish(uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.jobflow_file_stage(uuid,text,text) to service_role;
grant execute on function public.jobflow_file_put(uuid,text,jsonb) to service_role;
grant execute on function public.jobflow_file_gc_claim(uuid,text,integer) to service_role;
grant execute on function public.jobflow_file_gc_finish(uuid,text,jsonb) to service_role;
