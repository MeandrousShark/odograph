CREATE OR REPLACE FUNCTION public.assert_account_active(account bigint, version bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
BEGIN
 IF account IS DISTINCT FROM NULLIF(current_setting('app.account_id',true),'')::bigint THEN
  RAISE EXCEPTION 'account admission denied' USING ERRCODE='42501';
 END IF;
 PERFORM id FROM public.accounts
 WHERE id=account AND is_enabled AND auth_version=version FOR SHARE;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'account admission denied' USING ERRCODE='42501';
 END IF;
END
$body$;
