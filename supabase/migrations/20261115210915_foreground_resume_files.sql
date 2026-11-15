-- Immutable resume uploads are staged outside the worker lease. Their manifest
-- is published only with the checked record transaction, after bytes exist.
create or replace function public.jobflow_foreground_file_stage(p_workspace uuid,p_object_key text)
returns void language plpgsql security invoker set search_path='' as $$
begin
  perform 1 from jobflow_private.workspaces where id=p_workspace for update;
  if not found then raise exception 'Unknown workspace' using errcode='22023'; end if;
  if p_object_key is null or p_object_key !~ ('^' || p_workspace::text || '/[a-f0-9]{64}$') then
    raise exception 'Invalid resume object' using errcode='22023';
  end if;
  if exists(select 1 from jobflow_private.file_garbage where workspace_id=p_workspace
      and object_key=p_object_key and deleting) then
    raise exception 'Object cleanup is in progress; retry the save' using errcode='40001';
  end if;
  insert into jobflow_private.file_garbage(workspace_id,object_key)
    values(p_workspace,p_object_key)
    on conflict(workspace_id,object_key) do update set queued_at=now();
end $$;

create or replace function jobflow_private.publish_foreground_files(p_workspace uuid,p_files jsonb)
returns void language plpgsql security invoker set search_path='' as $$
declare f jsonb; previous jobflow_private.files;
begin
  if jsonb_typeof(p_files) is distinct from 'array' or jsonb_array_length(p_files)>100 then
    raise exception 'Invalid resume file list' using errcode='22023';
  end if;
  for f in select value from jsonb_array_elements(p_files) loop
    if f->>'path' is null or f->>'path' !~ '^inputs/application-resumes/resume-[a-f0-9]{32}$'
      or f->>'sha256' is null or f->>'sha256' !~ '^[a-f0-9]{64}$'
      or f->>'encoding' is distinct from 'identity'
      or f->>'object_key' is distinct from p_workspace::text || '/' || (f->>'sha256')
      or jsonb_typeof(f->'size') is distinct from 'number'
      or jsonb_typeof(f->'stored_size') is distinct from 'number'
      or (f->>'size')::bigint not between 1 and 5242880
      or (f->>'stored_size')::bigint is distinct from (f->>'size')::bigint then
      raise exception 'Invalid immutable resume manifest' using errcode='22023';
    end if;
    if exists(select 1 from jobflow_private.file_garbage where workspace_id=p_workspace
        and object_key=f->>'object_key' and deleting) then
      raise exception 'Object cleanup is in progress; retry the save' using errcode='40001';
    end if;
    if not exists(select 1 from jobflow_private.file_garbage where workspace_id=p_workspace
        and object_key=f->>'object_key') then
      raise exception 'Resume bytes must be staged before publication' using errcode='22023';
    end if;
    select * into previous from jobflow_private.files where workspace_id=p_workspace and path=f->>'path';
    if found and (previous.sha256 is distinct from f->>'sha256' or previous.size is distinct from (f->>'size')::bigint) then
      raise exception 'An immutable resume version already exists' using errcode='40001';
    end if;
    insert into jobflow_private.files(workspace_id,path,object_key,sha256,size,stored_size,encoding)
      values(p_workspace,f->>'path',f->>'object_key',f->>'sha256',(f->>'size')::bigint,
        (f->>'stored_size')::bigint,'identity') on conflict(workspace_id,path) do nothing;
  end loop;
end $$;

revoke all on function public.jobflow_foreground_file_stage(uuid,text) from public,anon,authenticated;
revoke all on function jobflow_private.publish_foreground_files(uuid,jsonb) from public,anon,authenticated;
grant execute on function public.jobflow_foreground_file_stage(uuid,text) to service_role;
grant execute on function jobflow_private.publish_foreground_files(uuid,jsonb) to service_role;
