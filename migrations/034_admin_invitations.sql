-- Add stable, nonsecret invitation IDs and protected administrator operations.
ALTER TABLE public.invitations ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE public.invitations ADD CONSTRAINT invitations_id_key UNIQUE (id);

-- Current protected invitation functions for role restore and validation.
CREATE OR REPLACE FUNCTION public.issue_member_invitation(
    input_admin_id bigint, input_auth_version bigint, input_email text, input_token_digest text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_email text := lower(btrim(input_email));
    issued_at timestamptz;
    invitation_id bigint;
BEGIN
    IF input_admin_id IS NULL OR input_auth_version IS NULL
       OR input_email IS NULL OR target_email = '' OR input_token_digest IS NULL
       OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM pg_catalog.pg_advisory_xact_lock(901411, pg_catalog.hashtext(input_admin_id::text));
    PERFORM 1 FROM public.accounts
    WHERE id = input_admin_id AND is_admin AND is_enabled
      AND auth_version = input_auth_version FOR SHARE;
    IF NOT FOUND OR EXISTS (SELECT 1 FROM public.accounts WHERE email = target_email) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    IF EXISTS (SELECT 1 FROM public.invitations
               WHERE email = target_email AND created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.invitations
           WHERE email = target_email AND created_at > issued_at - interval '24 hours') >= 5
       OR (SELECT count(*) FROM public.invitations
           WHERE issued_by = input_admin_id AND created_at > issued_at - interval '15 minutes') >= 10
       OR (SELECT count(*) FROM public.invitations
           WHERE issued_by = input_admin_id AND created_at > issued_at - interval '24 hours') >= 50 THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    UPDATE public.invitations SET revoked_at = pg_catalog.clock_timestamp()
    WHERE email = target_email AND consumed_at IS NULL AND revoked_at IS NULL;
    issued_at := pg_catalog.clock_timestamp();
    INSERT INTO public.invitations (token_digest, email, issued_by, created_at, expires_at)
    VALUES (input_token_digest, target_email, input_admin_id, issued_at, issued_at + interval '48 hours')
    RETURNING id INTO invitation_id;
    RETURN invitation_id;
END
$body$;
REVOKE ALL ON FUNCTION public.issue_member_invitation(bigint,bigint,text,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.resend_member_invitation(
    input_admin_id bigint, input_auth_version bigint,
    input_old_invitation_id bigint, input_token_digest text
) RETURNS TABLE (new_invitation_id bigint, invitation_email text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    old_email text;
    old_issuer_id bigint;
    old_invitation public.invitations%ROWTYPE;
    issued_at timestamptz;
BEGIN
    IF input_admin_id IS NULL OR input_auth_version IS NULL
       OR input_old_invitation_id IS NULL OR input_token_digest IS NULL
       OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    SELECT i.email, i.issued_by INTO old_email, old_issuer_id
    FROM public.invitations i WHERE i.id = input_old_invitation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(old_email));
    PERFORM pg_catalog.pg_advisory_xact_lock(901411, pg_catalog.hashtext(input_admin_id::text));
    PERFORM 1 FROM public.accounts a
    WHERE a.id IN (input_admin_id, old_issuer_id) ORDER BY a.id FOR SHARE;
    PERFORM 1 FROM public.accounts a
    WHERE a.id = input_admin_id AND a.is_admin AND a.is_enabled
      AND a.auth_version = input_auth_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.accounts a
    WHERE a.id = old_issuer_id AND a.is_admin AND a.is_enabled;
    IF NOT FOUND OR EXISTS (SELECT 1 FROM public.accounts a WHERE a.email = old_email) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO old_invitation FROM public.invitations i
    WHERE i.id = input_old_invitation_id FOR UPDATE;
    IF NOT FOUND OR old_invitation.email <> old_email
       OR old_invitation.issued_by <> old_issuer_id
       OR old_invitation.consumed_at IS NOT NULL OR old_invitation.revoked_at IS NOT NULL
       OR old_invitation.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    issued_at := pg_catalog.clock_timestamp();
    IF EXISTS (SELECT 1 FROM public.invitations i
               WHERE i.email = old_email AND i.created_at > issued_at - interval '1 minute')
       OR (SELECT count(*) FROM public.invitations i
           WHERE i.email = old_email AND i.created_at > issued_at - interval '24 hours') >= 5
       OR (SELECT count(*) FROM public.invitations i
           WHERE i.issued_by = input_admin_id AND i.created_at > issued_at - interval '15 minutes') >= 10
       OR (SELECT count(*) FROM public.invitations i
           WHERE i.issued_by = input_admin_id AND i.created_at > issued_at - interval '24 hours') >= 50 THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    UPDATE public.invitations SET revoked_at = pg_catalog.clock_timestamp()
    WHERE invitations.id = input_old_invitation_id;
    issued_at := pg_catalog.clock_timestamp();
    INSERT INTO public.invitations (token_digest, email, issued_by, created_at, expires_at)
    VALUES (input_token_digest, old_email, input_admin_id, issued_at, issued_at + interval '48 hours')
    RETURNING invitations.id INTO new_invitation_id;
    invitation_email := old_email;
    RETURN NEXT;
END
$body$;
REVOKE ALL ON FUNCTION public.resend_member_invitation(bigint,bigint,bigint,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.revoke_member_invitation(
    input_admin_id bigint, input_auth_version bigint, input_invitation_id bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_email text;
    issuer_id bigint;
    invitation_row public.invitations%ROWTYPE;
BEGIN
    IF input_admin_id IS NULL OR input_auth_version IS NULL OR input_invitation_id IS NULL THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    SELECT email, issued_by INTO target_email, issuer_id FROM public.invitations
    WHERE id = input_invitation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_admin_id, issuer_id) ORDER BY id FOR SHARE;
    PERFORM 1 FROM public.accounts
    WHERE id = input_admin_id AND is_admin AND is_enabled
      AND auth_version = input_auth_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO invitation_row FROM public.invitations WHERE id = input_invitation_id FOR UPDATE;
    IF NOT FOUND OR invitation_row.email <> target_email
       OR invitation_row.issued_by <> issuer_id THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    IF invitation_row.consumed_at IS NULL AND invitation_row.revoked_at IS NULL THEN
        UPDATE public.invitations SET revoked_at = pg_catalog.clock_timestamp()
        WHERE id = input_invitation_id;
    END IF;
END
$body$;
REVOKE ALL ON FUNCTION public.revoke_member_invitation(bigint,bigint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.list_member_invitations(
    input_admin_id bigint, input_auth_version bigint
) RETURNS TABLE (id bigint, email text, issued_by bigint, created_at timestamptz,
                 expires_at timestamptz, consumed_at timestamptz, revoked_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    PERFORM 1 FROM public.accounts
    WHERE accounts.id = input_admin_id AND is_admin AND is_enabled
      AND auth_version = input_auth_version FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT i.id, i.email, i.issued_by, i.created_at, i.expires_at,
                        i.consumed_at, i.revoked_at
    FROM public.invitations i ORDER BY i.created_at DESC, i.id DESC;
END
$body$;
REVOKE ALL ON FUNCTION public.list_member_invitations(bigint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.admit_member_invitation_send(
    input_admin_id bigint, input_auth_version bigint, input_invitation_id bigint
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_email text;
    issuer_id bigint;
    invitation_row public.invitations%ROWTYPE;
BEGIN
    IF input_admin_id IS NULL OR input_auth_version IS NULL OR input_invitation_id IS NULL THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    SELECT email, issued_by INTO target_email, issuer_id FROM public.invitations
    WHERE id = input_invitation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM 1 FROM public.accounts
    WHERE id IN (input_admin_id, issuer_id) ORDER BY id FOR SHARE;
    PERFORM 1 FROM public.accounts
    WHERE id = input_admin_id AND is_admin AND is_enabled
      AND auth_version = input_auth_version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.accounts
    WHERE id = issuer_id AND is_admin AND is_enabled;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO invitation_row FROM public.invitations WHERE id = input_invitation_id FOR UPDATE;
    IF NOT FOUND OR invitation_row.email <> target_email
       OR invitation_row.issued_by <> issuer_id
       OR invitation_row.consumed_at IS NOT NULL OR invitation_row.revoked_at IS NOT NULL
       OR invitation_row.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    RETURN invitation_row.email;
END
$body$;
REVOKE ALL ON FUNCTION public.admit_member_invitation_send(bigint,bigint,bigint) FROM PUBLIC;


DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER SEQUENCE public.invitations_id_seq OWNER TO odograph_migrate;
        REVOKE ALL ON SEQUENCE public.invitations_id_seq FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        GRANT USAGE ON SEQUENCE public.invitations_id_seq TO odograph_bootstrap;
        REVOKE EXECUTE ON FUNCTION public.issue_member_invitation(bigint,text,text) FROM odograph_control;
        ALTER FUNCTION public.issue_member_invitation(bigint,bigint,text,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.resend_member_invitation(bigint,bigint,bigint,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.revoke_member_invitation(bigint,bigint,bigint) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.list_member_invitations(bigint,bigint) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.admit_member_invitation_send(bigint,bigint,bigint) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.issue_member_invitation(bigint,bigint,text,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.resend_member_invitation(bigint,bigint,bigint,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.revoke_member_invitation(bigint,bigint,bigint) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.list_member_invitations(bigint,bigint) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.admit_member_invitation_send(bigint,bigint,bigint) TO odograph_control;
    END IF;
END $$;
