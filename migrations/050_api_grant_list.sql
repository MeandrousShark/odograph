-- migration-class: additive
-- Signed-in apps list: the display fields of one account's active grants.
-- A grant is active while it is not revoked, not idle-expired (60 days after
-- the later of its creation and last use) and issued at the account's current
-- auth_version. Digests and token generations never leave the table.
CREATE FUNCTION public.api_grant_list(input_account_id bigint)
RETURNS TABLE (result_public_id text, result_device_label text,
               result_created_at timestamptz, result_last_used_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT g.public_id, g.device_label, g.created_at, g.last_used_at
    FROM public.api_grants g
    JOIN public.accounts a ON a.id = g.account_id
    WHERE g.account_id = input_account_id
      AND a.is_enabled
      AND a.auth_version = g.issued_auth_version
      AND g.revoked_at IS NULL
      AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at))
          > pg_catalog.clock_timestamp() - interval '60 days'
    ORDER BY g.created_at DESC, g.id DESC
$body$;

REVOKE ALL ON FUNCTION public.api_grant_list(bigint) FROM PUBLIC;

DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER FUNCTION public.api_grant_list(bigint) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.api_grant_list(bigint)
            FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.api_grant_list(bigint) TO odograph_control;
    END IF;
END
$permissions$;
