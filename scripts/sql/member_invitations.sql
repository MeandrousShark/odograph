-- Current protected invitation functions for role restore and validation.
CREATE OR REPLACE FUNCTION public.issue_member_invitation(
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

CREATE OR REPLACE FUNCTION public.redeem_member_invitation(
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

CREATE OR REPLACE FUNCTION public.redeem_oidc_member_invitation(
    input_token_digest text, input_issuer text, input_subject text,
    input_provider_email text, input_provider_display_name text, input_display_tz text
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
       OR input_issuer IS NULL OR input_issuer = '' OR input_issuer <> pg_catalog.rtrim(input_issuer, '/')
       OR input_subject IS NULL OR input_subject = ''
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = input_display_tz) THEN
        RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023';
    END IF;
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
    VALUES (invitation_row.email, NULL, false)
    RETURNING id INTO member_id;
    INSERT INTO public.oidc_identities
        (account_id, issuer, subject, provider_email, provider_display_name)
    VALUES (member_id, input_issuer, input_subject, input_provider_email, input_provider_display_name);
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
REVOKE ALL ON FUNCTION public.redeem_oidc_member_invitation(text,text,text,text,text,text) FROM PUBLIC;
