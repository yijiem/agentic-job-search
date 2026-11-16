-- Synthetic concurrency/privilege checks. All records and file manifests roll back.
-- When testing pending migrations, wrap their SQL and this body in ONE outer
-- transaction (omit this file's BEGIN/ROLLBACK in that combined script).
begin;
set local role service_role;
do $$
declare
  account jsonb; w uuid; account_id uuid; token text=repeat('t',43);
  now_text text='2026-11-15T12:00:00-08:00'; answer jsonb; denied boolean;
  task_before jsonb='{"id":"task","status":"todo","notes":""}'::jsonb;
  task_after jsonb='{"id":"task","status":"done","notes":"Keep my completed work","completed_at":"2026-11-15T12:00:00-08:00"}'::jsonb;
  task_row jsonb; app_row jsonb; plan_row jsonb; batch_row jsonb; resume_row jsonb; metadata jsonb;
  object_key text; resume_path text='inputs/application-resumes/resume-'||repeat('a',32);
begin
  if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where p.proname in ('jobflow_records_compare_and_set','jobflow_foreground_file_stage','publish_foreground_files')
      and (p.prosecdef or has_function_privilege('anon',p.oid,'EXECUTE')
        or has_function_privilege('authenticated',p.oid,'EXECUTE'))) then
    raise exception 'Foreground RPCs exposed privileged access';
  end if;
  account=public.jobflow_account_invite('foreground-test-'||gen_random_uuid()::text||'@example.test');
  w=(account->>'workspace_id')::uuid; account_id=(account->>'id')::uuid;
  perform public.jobflow_workspace_create(w);
  if not public.jobflow_workspace_lease(w,token,300,'acquire') then raise exception 'Could not acquire test worker lease'; end if;
  task_row=jsonb_build_object('kind','tasks','id','task','body',task_before,'created_at',now_text,'updated_at',now_text);
  perform public.jobflow_records_put(w,token,jsonb_build_array(task_row));
  app_row=jsonb_build_object('kind','applications','id','saved','body','{"id":"saved","stage":"saved"}'::jsonb,
    'created_at',now_text,'updated_at',now_text);
  task_row=jsonb_set(task_row,'{body}',task_after);
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(app_row,task_row),
    jsonb_build_array(jsonb_build_object('kind','applications','id','saved','body',null),
      jsonb_build_object('kind','tasks','id','task','body',task_before)),
    '[]',true,null,'[]',true);
  if answer->>'ok'<>'true' then raise exception 'Foreground write failed while worker lease was held: %',answer; end if;
  if (select lease_token from jobflow_private.workspaces where id=w)<>token then raise exception 'Foreground replaced worker lease'; end if;
  if public.jobflow_record_get(w,'tasks','task') is distinct from task_after then raise exception 'Completion not preserved'; end if;
  if public.jobflow_queue_status(account_id)->>'command'<>'refresh' then raise exception 'Atomic refresh request missing'; end if;

  -- A pre-migration worker's unconditional write may no longer overwrite user data.
  denied=false;
  begin
    perform public.jobflow_records_put(w,token,jsonb_build_array(jsonb_set(task_row,'{body}',task_before)));
  exception when serialization_failure then denied=true; end;
  if not denied then raise exception 'Legacy worker overwrote foreground completion'; end if;
  perform public.jobflow_records_put(w,token,jsonb_build_array(jsonb_build_object('kind','jobs','id','job',
    'body','{"id":"job","availability":"open"}'::jsonb,'created_at',now_text,'updated_at',now_text)));

  plan_row=jsonb_build_object('kind','plans','id','current','body','{"id":"current"}'::jsonb,
    'created_at',now_text,'updated_at',now_text);
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(task_row,plan_row),
    jsonb_build_array(jsonb_build_object('kind','tasks','id','task','body',task_before),
      jsonb_build_object('kind','plans','id','current','body',null)),'[]',false,token);
  if answer->>'conflict'<>'true' or public.jobflow_record_get(w,'plans','current') is not null then
    raise exception 'Stale derived update was partially committed';
  end if;
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(plan_row),
    jsonb_build_array(jsonb_build_object('kind','tasks','id','task','body',task_after),
      jsonb_build_object('kind','plans','id','current','body',null)),'[]',false,token);
  if answer->>'ok'<>'true' then raise exception 'Fresh checked worker could not publish'; end if;

  batch_row=jsonb_build_object('kind','application_batches','id','first','body','{"id":"first"}'::jsonb,
    'created_at',now_text,'updated_at',now_text);
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(batch_row),
    '[{"kind":"application_batches","id":"first","body":null}]',
    '[{"kind":"application_batches","ids":[]}]',true);
  if answer->>'ok'<>'true' then raise exception 'Empty collection expectation failed'; end if;
  batch_row=jsonb_set(jsonb_set(batch_row,'{id}','"second"'),'{body}','{"id":"second"}');
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(batch_row),
    '[{"kind":"application_batches","id":"second","body":null}]',
    '[{"kind":"application_batches","ids":[]}]',true);
  if answer->>'conflict'<>'true' or public.jobflow_record_get(w,'application_batches','second') is not null then
    raise exception 'Collection phantom permitted duplicate batch';
  end if;

  -- A failed record CAS cannot publish a resume manifest; a successful CAS
  -- publishes the new immutable manifest and record in the same transaction.
  object_key=w::text||'/'||repeat('b',64);
  perform public.jobflow_foreground_file_stage(w,object_key);
  metadata=jsonb_build_object('path',resume_path,'object_key',object_key,'sha256',repeat('b',64),
    'size',100,'stored_size',100,'encoding','identity');
  resume_row=jsonb_build_object('kind','application_resumes','id','resume','body','{"id":"resume"}'::jsonb,
    'created_at',now_text,'updated_at',now_text);
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(resume_row),
    jsonb_build_array(jsonb_build_object('kind','application_resumes','id','resume','body',null),
      jsonb_build_object('kind','tasks','id','task','body',task_before)),
    '[]',true,null,jsonb_build_array(metadata));
  if answer->>'conflict'<>'true' or exists(select 1 from jobflow_private.files where workspace_id=w and path=resume_path)
    or public.jobflow_record_get(w,'application_resumes','resume') is not null then
    raise exception 'Failed transaction published resume files or records';
  end if;
  answer=public.jobflow_records_compare_and_set(w,jsonb_build_array(resume_row),
    '[{"kind":"application_resumes","id":"resume","body":null}]','[]',true,null,jsonb_build_array(metadata));
  if answer->>'ok'<>'true' or not exists(select 1 from jobflow_private.files where workspace_id=w and path=resume_path)
    or public.jobflow_record_get(w,'application_resumes','resume') is null then
    raise exception 'Atomic resume publication failed';
  end if;
  denied=false;
  perform public.jobflow_foreground_file_stage(w,w::text||'/'||repeat('c',64));
  begin
    perform public.jobflow_records_compare_and_set(w,'[]','[]','[]',true,null,
      jsonb_build_array(jsonb_set(jsonb_set(metadata,'{sha256}',to_jsonb(repeat('c',64))),
        '{object_key}',to_jsonb(w::text||'/'||repeat('c',64)))));
  exception when serialization_failure then denied=true; end;
  if not denied then raise exception 'Immutable resume manifest could be overwritten'; end if;

  denied=false;
  begin
    perform public.jobflow_records_compare_and_set(w,'[]','[]','[]',false,repeat('x',43));
  exception when lock_not_available then denied=true; end;
  if not denied then raise exception 'Wrong background token accepted'; end if;
  denied=false;
  begin
    perform public.jobflow_records_compare_and_set(w,
      jsonb_build_array(jsonb_build_object('kind','jobs','id','forbidden','body','{"id":"forbidden"}'::jsonb,
        'created_at',now_text,'updated_at',now_text)),'[]','[]',true);
  exception when invalid_parameter_value then denied=true; end;
  if not denied then raise exception 'Foreground ingested jobs'; end if;
  denied=false;
  begin
    perform public.jobflow_records_compare_and_set(w,jsonb_build_array(app_row),'[]','[]',true);
  exception when invalid_parameter_value then denied=true; end;
  if not denied then raise exception 'Shared write without expected value accepted'; end if;
end $$;
set local role anon;
do $$
declare denied boolean=false;
begin
  begin perform public.jobflow_records_compare_and_set(gen_random_uuid(),'[]','[]','[]',true);
  exception when insufficient_privilege then denied=true; end;
  if not denied then raise exception 'Anonymous role can execute foreground mutation'; end if;
end $$;
set local role authenticated;
do $$
declare denied boolean=false;
begin
  begin perform public.jobflow_foreground_file_stage(gen_random_uuid(),'invalid');
  exception when insufficient_privilege then denied=true; end;
  if not denied then raise exception 'Authenticated browser role can stage private files'; end if;
end $$;
rollback;
