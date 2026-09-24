CREATE TABLE invitations (
    token_digest text PRIMARY KEY CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    email text NOT NULL CHECK (email = lower(btrim(email)) AND email <> ''),
    issued_by bigint NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    revoked_at timestamptz,
    CHECK (expires_at > created_at),
    CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX invitations_one_outstanding_email_idx
    ON invitations (email) WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE FUNCTION public.issue_member_invitation(
    input_admin_id bigint, input_email text, input_token_digest text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    target_email text := lower(btrim(input_email));
    issued_at timestamptz;
BEGIN
    IF input_email IS NULL OR target_email = '' OR input_token_digest IS NULL
       OR input_token_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM 1 FROM public.accounts
    WHERE id = input_admin_id AND is_admin AND is_enabled FOR SHARE;
    IF NOT FOUND OR EXISTS (SELECT 1 FROM public.accounts WHERE email = target_email) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    UPDATE public.invitations SET revoked_at = pg_catalog.clock_timestamp()
    WHERE email = target_email AND consumed_at IS NULL AND revoked_at IS NULL;
    issued_at := pg_catalog.clock_timestamp();
    INSERT INTO public.invitations (token_digest, email, issued_by, created_at, expires_at)
    VALUES (input_token_digest, target_email, input_admin_id, issued_at, issued_at + interval '48 hours');
END
$body$;
REVOKE ALL ON FUNCTION public.issue_member_invitation(bigint,text,text) FROM PUBLIC;

CREATE FUNCTION public.redeem_member_invitation(
    input_token_digest text, input_password_hash text, input_display_tz text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    invitation_row public.invitations%ROWTYPE;
    issuer_id bigint;
    target_email text;
    member_id bigint;
BEGIN
    IF input_token_digest IS NULL OR input_token_digest !~ '^[0-9a-f]{64}$'
       OR input_password_hash IS NULL OR input_password_hash = ''
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = input_display_tz) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
    -- Every invitation transition takes email, issuer, then invitation locks.
    -- Recheck the token after the locks because this first read is unlocked.
    SELECT issued_by, email INTO issuer_id, target_email FROM public.invitations
    WHERE token_digest = input_token_digest;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(901410, pg_catalog.hashtext(target_email));
    PERFORM 1 FROM public.accounts
    WHERE id = issuer_id AND is_admin AND is_enabled FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO invitation_row FROM public.invitations
    WHERE token_digest = input_token_digest FOR UPDATE;
    IF NOT FOUND OR invitation_row.issued_by <> issuer_id
       OR invitation_row.email <> target_email
       OR invitation_row.consumed_at IS NOT NULL OR invitation_row.revoked_at IS NOT NULL
       OR invitation_row.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.accounts (email, password_hash, is_admin)
    VALUES (invitation_row.email, input_password_hash, false)
    RETURNING id INTO member_id;
    INSERT INTO public.account_settings (account_id, display_tz)
    VALUES (member_id, input_display_tz);
    INSERT INTO public.vehicles (account_id, name, is_default)
    VALUES (member_id, 'My Car', true);
    INSERT INTO public.tag_rules (account_id, a_kind, b_kind, category)
    VALUES (member_id, 'home', 'work', 'personal'),
           (member_id, 'work', 'work', 'business');
    INSERT INTO public.mileage_rates (account_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month)
    SELECT member_id, year, rate_per_mi, rate_h2_per_mi, h2_start_month
    FROM public.reference_mileage_rates;
    UPDATE public.invitations SET consumed_at = pg_catalog.clock_timestamp()
    WHERE token_digest = input_token_digest;
    RETURN member_id;
END
$body$;
REVOKE ALL ON FUNCTION public.redeem_member_invitation(text,text,text) FROM PUBLIC;

-- Fresh installations receive these grants during role setup. Existing
-- installations need the contract in this same migration transaction.
DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.invitations OWNER TO odograph_migrate;
        REVOKE ALL ON public.invitations FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        GRANT SELECT, INSERT, UPDATE ON public.invitations TO odograph_bootstrap;
        ALTER FUNCTION public.issue_member_invitation(bigint,text,text) OWNER TO odograph_bootstrap;
        ALTER FUNCTION public.redeem_member_invitation(text,text,text) OWNER TO odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.issue_member_invitation(bigint,text,text) TO odograph_control;
        GRANT EXECUTE ON FUNCTION public.redeem_member_invitation(text,text,text) TO odograph_control;
    END IF;
END $$;
