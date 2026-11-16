-- Canonical workspace records. Only the server role can invoke these RPCs.
create schema if not exists jobflow_private;
revoke all on schema jobflow_private from public, anon, authenticated;
grant usage on schema jobflow_private to service_role;

create table if not exists jobflow_private.workspaces (
  id uuid primary key,
  created_at timestamptz not null default clock_timestamp(),
  lease_token text,
  lease_until timestamptz,
  revision bigint not null default 0
);
create table if not exists jobflow_private.records (
  workspace_id uuid not null references jobflow_private.workspaces(id),
  kind text not null,
  id text not null,
  body jsonb not null check (jsonb_typeof(body) = 'object'),
  created_at text not null,
  updated_at text not null,
  primary key (workspace_id,kind,id)
);
create index if not exists records_workspace_order
  on jobflow_private.records(workspace_id,kind,created_at,id);
create table if not exists jobflow_private.runs (
  workspace_id uuid not null references jobflow_private.workspaces(id),
  id bigint not null,
  started_at text not null,
  finished_at text,
  status text not null,
  details text not null,
  primary key (workspace_id,id)
);
alter table jobflow_private.workspaces enable row level security;
alter table jobflow_private.records enable row level security;
alter table jobflow_private.runs enable row level security;
revoke all on jobflow_private.workspaces, jobflow_private.records, jobflow_private.runs
  from public, anon, authenticated;
grant select,insert,update,delete on jobflow_private.workspaces, jobflow_private.records,
  jobflow_private.runs to service_role;

create or replace function jobflow_private.assert_lease(p_workspace uuid,p_token text)
returns void language plpgsql security invoker set search_path = '' as $$
declare active boolean;
begin
  select w.lease_token=p_token and w.lease_until>clock_timestamp() into active
    from jobflow_private.workspaces w where w.id=p_workspace for update;
  if p_token is null or active is distinct from true then
    raise exception 'Workspace lease is absent or expired' using errcode='55P03';
  end if;
end $$;
revoke all on function jobflow_private.assert_lease(uuid,text) from public,anon,authenticated;
grant execute on function jobflow_private.assert_lease(uuid,text) to service_role;

create or replace function public.jobflow_workspace_create(p_workspace uuid)
returns void language sql security invoker set search_path = '' as $$
  insert into jobflow_private.workspaces(id) values(p_workspace) on conflict(id) do nothing;
$$;
create or replace function public.jobflow_workspace_lease(
  p_workspace uuid,p_token text,p_seconds integer,p_action text)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare affected integer;
begin
  if p_token is null or p_token !~ '^[A-Za-z0-9_-]{32,128}$'
     or p_seconds not between 30 and 3600 then
    raise exception 'Invalid lease request' using errcode='22023';
  end if;
  if p_action='acquire' then
    update jobflow_private.workspaces set lease_token=p_token,
      lease_until=clock_timestamp()+make_interval(secs=>p_seconds)
      where id=p_workspace and (lease_until is null or lease_until<=clock_timestamp());
  elsif p_action='renew' then
    update jobflow_private.workspaces set lease_until=clock_timestamp()+make_interval(secs=>p_seconds)
      where id=p_workspace and lease_token=p_token and lease_until>clock_timestamp();
  elsif p_action='release' then
    update jobflow_private.workspaces set lease_token=null,lease_until=null
      where id=p_workspace and lease_token=p_token;
  else
    raise exception 'Invalid lease action' using errcode='22023';
  end if;
  get diagnostics affected=row_count;
  return affected=1;
end $$;

create or replace function public.jobflow_record_get(p_workspace uuid,p_kind text,p_id text)
returns jsonb language sql stable security invoker set search_path = '' as $$
  select r.body from jobflow_private.records r
   where r.workspace_id=p_workspace and r.kind=p_kind and r.id=p_id;
$$;

create or replace function public.jobflow_records_page(
  p_workspace uuid,p_kind text default null,p_mode text default 'all',
  p_minimum numeric default 40,p_job text default null,
  p_after_kind text default '',p_after_created text default '',p_after_id text default '',
  p_limit integer default 200,p_revision bigint default null)
returns jsonb language sql stable security invoker set search_path = '' as $$
  with workspace as (
    select w.revision from jobflow_private.workspaces w where w.id=p_workspace
  ), selected as (
    select r.kind,r.id,r.created_at,r.updated_at,
      case when r.kind<>'jobs' or p_mode in ('all','detail') then r.body
           when p_mode='ids' then jsonb_build_object('id',r.id)
           else r.body-'description' end as body
    from jobflow_private.records r
    where r.workspace_id=p_workspace and (p_kind is null or r.kind=p_kind)
      and (r.kind,r.created_at,r.id)>(p_after_kind,p_after_created,p_after_id)
      and (p_revision is null or p_revision=(select revision from workspace))
      and (r.kind<>'jobs' or p_mode in ('all','ids','projected')
        or (p_mode='detail' and r.id=p_job)
        or (p_mode='matching' and (
          (r.body->'eligible'='true'::jsonb and
           case when jsonb_typeof(r.body->'score')='number' then (r.body->>'score')::numeric else 0 end>=p_minimum)
          or exists(select 1 from jobflow_private.records t
            where t.workspace_id=p_workspace and t.kind in ('applications','interviews')
              and t.body->>'job_id'=r.id))))
    order by r.kind,r.created_at,r.id
    limit greatest(1,least(p_limit,500))
  )
  select jsonb_build_object(
    'revision',coalesce((select revision from workspace),0),
    'conflict',p_revision is not null and p_revision is distinct from (select revision from workspace),
    'records',coalesce((select jsonb_agg(to_jsonb(s) order by s.kind,s.created_at,s.id) from selected s),'[]'::jsonb),
    'count',(select count(*) from jobflow_private.records r where r.workspace_id=p_workspace and r.kind='jobs'),
    'runs',case when p_after_kind='' then coalesce((select jsonb_agg(to_jsonb(x) order by x.id desc) from (
      select id,started_at,finished_at,status,details from jobflow_private.runs
      where workspace_id=p_workspace order by id desc limit 30) x),'[]'::jsonb) else '[]'::jsonb end);
$$;

create or replace function public.jobflow_records_put(
  p_workspace uuid,p_token text,p_rows jsonb,p_import boolean default false)
returns integer language plpgsql security invoker set search_path = '' as $$
declare affected integer;
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if jsonb_typeof(p_rows)<>'array' or jsonb_array_length(p_rows)>10000 then
    raise exception 'Invalid record batch' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_rows)
      as x(kind text,id text,body jsonb,created_at text,updated_at text)
      where x.kind is null or x.kind !~ '^[a-z][a-z0-9_]{0,63}$'
       or x.id is null or length(x.id) not between 1 and 512
       or jsonb_typeof(x.body) is distinct from 'object'
       or x.created_at is null or x.updated_at is null) then
    raise exception 'Invalid record' using errcode='22023';
  end if;
  if p_import and exists(select 1 from jsonb_to_recordset(p_rows)
      as x(kind text,id text,body jsonb,created_at text,updated_at text)
      join jobflow_private.records r on r.workspace_id=p_workspace and r.kind=x.kind and r.id=x.id
      where (r.body,r.created_at,r.updated_at) is distinct from (x.body,x.created_at,x.updated_at)) then
    raise exception 'Import conflicts with existing records' using errcode='22023';
  end if;
  insert into jobflow_private.records(workspace_id,kind,id,body,created_at,updated_at)
    select p_workspace,x.kind,x.id,x.body,x.created_at,x.updated_at
    from jsonb_to_recordset(p_rows) as x(kind text,id text,body jsonb,created_at text,updated_at text)
    on conflict(workspace_id,kind,id) do update set
      body=excluded.body,updated_at=excluded.updated_at;
  get diagnostics affected=row_count;
  if affected>0 then
    update jobflow_private.workspaces set revision=revision+1 where id=p_workspace;
  end if;
  return affected;
end $$;

create or replace function public.jobflow_run_start(p_workspace uuid,p_token text,p_started_at text)
returns bigint language plpgsql security invoker set search_path = '' as $$
declare next_id bigint;
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  select coalesce(max(id),0)+1 into next_id from jobflow_private.runs where workspace_id=p_workspace;
  insert into jobflow_private.runs(workspace_id,id,started_at,status,details)
    values(p_workspace,next_id,p_started_at,'running','{}');
  update jobflow_private.workspaces set revision=revision+1 where id=p_workspace;
  return next_id;
end $$;
create or replace function public.jobflow_run_finish(
  p_workspace uuid,p_token text,p_id bigint,p_finished_at text,p_status text,p_details text)
returns void language plpgsql security invoker set search_path = '' as $$
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  update jobflow_private.runs set finished_at=p_finished_at,status=p_status,details=p_details
    where workspace_id=p_workspace and id=p_id;
  update jobflow_private.workspaces set revision=revision+1 where id=p_workspace;
end $$;
create or replace function public.jobflow_runs_recent(p_workspace uuid,p_limit integer default 10)
returns jsonb language sql stable security invoker set search_path = '' as $$
 select coalesce(jsonb_agg(to_jsonb(x) order by x.id desc),'[]'::jsonb) from (
   select id,started_at,finished_at,status,details from jobflow_private.runs
   where workspace_id=p_workspace order by id desc limit greatest(0,least(p_limit,10000))) x;
$$;
create or replace function public.jobflow_runs_import(p_workspace uuid,p_token text,p_rows jsonb)
returns integer language plpgsql security invoker set search_path = '' as $$
declare affected integer;
begin
  perform jobflow_private.assert_lease(p_workspace,p_token);
  if jsonb_typeof(p_rows)<>'array' or jsonb_array_length(p_rows)>1000 then
    raise exception 'Invalid run batch' using errcode='22023';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_rows)
      as x(id bigint,started_at text,finished_at text,status text,details text)
      join jobflow_private.runs r on r.workspace_id=p_workspace and r.id=x.id
      where (r.started_at,r.finished_at,r.status,r.details)
        is distinct from (x.started_at,x.finished_at,x.status,x.details)) then
    raise exception 'Import conflicts with existing runs' using errcode='22023';
  end if;
  insert into jobflow_private.runs(workspace_id,id,started_at,finished_at,status,details)
    select p_workspace,x.id,x.started_at,x.finished_at,x.status,x.details
    from jsonb_to_recordset(p_rows) as x(id bigint,started_at text,finished_at text,status text,details text)
    on conflict(workspace_id,id) do nothing;
  get diagnostics affected=row_count;
  if affected>0 then update jobflow_private.workspaces set revision=revision+1 where id=p_workspace; end if;
  return affected;
end $$;
create or replace function public.jobflow_workspace_stats(p_workspace uuid)
returns jsonb language sql stable security invoker set search_path = '' as $$
  select jsonb_build_object(
    'revision',(select revision from jobflow_private.workspaces where id=p_workspace),
    'records',coalesce((select jsonb_object_agg(kind,n) from (
      select kind,count(*) as n from jobflow_private.records where workspace_id=p_workspace group by kind) x),'{}'::jsonb),
    'runs',(select count(*) from jobflow_private.runs where workspace_id=p_workspace));
$$;
create or replace function public.jobflow_import_verify(
 p_workspace uuid,p_records jsonb default '[]',p_runs jsonb default '[]')
returns jsonb language sql stable security invoker set search_path = '' as $$
 select jsonb_build_object(
  'records_mismatched',(select count(*) from jsonb_to_recordset(p_records)
    as x(kind text,id text,body jsonb,created_at text,updated_at text)
    left join jobflow_private.records r on r.workspace_id=p_workspace and r.kind=x.kind and r.id=x.id
    where (r.body,r.created_at,r.updated_at) is distinct from (x.body,x.created_at,x.updated_at)),
  'runs_mismatched',(select count(*) from jsonb_to_recordset(p_runs)
    as x(id bigint,started_at text,finished_at text,status text,details text)
    left join jobflow_private.runs r on r.workspace_id=p_workspace and r.id=x.id
    where r.id is null or (r.started_at,r.finished_at,r.status,r.details)
      is distinct from (x.started_at,x.finished_at,x.status,x.details)));
$$;

-- Revoke PostgreSQL's default PUBLIC EXECUTE, including for future caller roles.
revoke all on function public.jobflow_workspace_create(uuid) from public,anon,authenticated;
revoke all on function public.jobflow_workspace_lease(uuid,text,integer,text) from public,anon,authenticated;
revoke all on function public.jobflow_record_get(uuid,text,text) from public,anon,authenticated;
revoke all on function public.jobflow_records_page(uuid,text,text,numeric,text,text,text,text,integer,bigint) from public,anon,authenticated;
revoke all on function public.jobflow_records_put(uuid,text,jsonb,boolean) from public,anon,authenticated;
revoke all on function public.jobflow_run_start(uuid,text,text) from public,anon,authenticated;
revoke all on function public.jobflow_run_finish(uuid,text,bigint,text,text,text) from public,anon,authenticated;
revoke all on function public.jobflow_runs_recent(uuid,integer) from public,anon,authenticated;
revoke all on function public.jobflow_runs_import(uuid,text,jsonb) from public,anon,authenticated;
revoke all on function public.jobflow_workspace_stats(uuid) from public,anon,authenticated;
revoke all on function public.jobflow_import_verify(uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.jobflow_workspace_create(uuid),
 public.jobflow_workspace_lease(uuid,text,integer,text),
 public.jobflow_record_get(uuid,text,text),
 public.jobflow_records_page(uuid,text,text,numeric,text,text,text,text,integer,bigint),
 public.jobflow_records_put(uuid,text,jsonb,boolean),
 public.jobflow_run_start(uuid,text,text),
 public.jobflow_run_finish(uuid,text,bigint,text,text,text),
 public.jobflow_runs_recent(uuid,integer),
 public.jobflow_runs_import(uuid,text,jsonb),
 public.jobflow_workspace_stats(uuid), public.jobflow_import_verify(uuid,jsonb,jsonb)
 to service_role;
