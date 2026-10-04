CREATE FUNCTION public.assert_import_account_exclusive(account bigint, version bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 IF account IS DISTINCT FROM NULLIF(current_setting('app.account_id',true),'')::bigint THEN
  RAISE EXCEPTION 'import admission denied' USING ERRCODE='42501';
 END IF;
 PERFORM id FROM public.accounts
 WHERE id=account AND is_enabled AND auth_version=version FOR UPDATE NOWAIT;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'import admission denied' USING ERRCODE='42501';
 END IF;
END
$body$;
REVOKE ALL ON FUNCTION public.assert_import_account_exclusive(bigint,bigint)
FROM PUBLIC;

DO $$
BEGIN
 IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
  ALTER FUNCTION public.assert_import_account_exclusive(bigint,bigint) OWNER TO odograph_migrate;
  REVOKE ALL ON FUNCTION public.assert_import_account_exclusive(bigint,bigint)
  FROM odograph_control,odograph_runtime,odograph_bootstrap;
  GRANT EXECUTE ON FUNCTION public.assert_import_account_exclusive(bigint,bigint) TO odograph_runtime;
 END IF;
END $$;
