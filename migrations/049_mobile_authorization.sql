-- migration-class: additive
-- Mobile sign-in: a pending authorization request started by a signed-out or
-- signed-in browser, and the one-time code an approval issues. Random values
-- exist in plaintext only in the browser cookie and the callback; the
-- database stores SHA-256 digests.
--
-- A pending request has no account until approval, like oidc_attempts, so it
-- is control state without row-level security. A code is bound to the
-- approving account and is protected like api_grants. Neither is charged to
-- storage and neither has an admission trigger. No application role has
-- table rights; the functions below are the only access.
CREATE TABLE public.mobile_authorization_requests (
    request_digest text PRIMARY KEY CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    client_id text NOT NULL CHECK (client_id ~ '^[a-z0-9-]{1,64}$'),
    redirect_uri text NOT NULL CHECK (char_length(redirect_uri) BETWEEN 1 AND 256),
    code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
    state text NOT NULL CHECK (state ~ '^[!-~]+$' AND char_length(state) <= 256),
    device_label text NOT NULL CHECK (device_label <> '' AND char_length(device_label) <= 64),
    replaces text CHECK (replaces ~ '^[A-Za-z0-9_-]{22}$'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX mobile_authorization_requests_expiry_idx
    ON public.mobile_authorization_requests (expires_at);

CREATE TABLE public.mobile_authorization_codes (
    code_digest text PRIMARY KEY CHECK (code_digest ~ '^[0-9a-f]{64}$'),
    account_id bigint NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    issued_auth_version bigint NOT NULL CHECK (issued_auth_version > 0),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    client_id text NOT NULL CHECK (client_id ~ '^[a-z0-9-]{1,64}$'),
    code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
    device_label text NOT NULL CHECK (device_label <> '' AND char_length(device_label) <= 64),
    replaces text CHECK (replaces ~ '^[A-Za-z0-9_-]{22}$'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX mobile_authorization_codes_account_idx ON public.mobile_authorization_codes (account_id);
CREATE INDEX mobile_authorization_codes_expiry_idx ON public.mobile_authorization_codes (expires_at);

ALTER TABLE public.mobile_authorization_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mobile_authorization_codes FORCE ROW LEVEL SECURITY;

-- Lifetimes: a pending request lasts 10 minutes and a code 60 seconds. At
-- most 1000 live pending requests exist at once, because a signed-out
-- browser can create them. A new request from the same browser (its
-- previous cookie) replaces the old one. Returns the expiry, or NULL when the
-- input is invalid or the cap is reached.
CREATE FUNCTION public.mobile_authorization_start(
    input_request_digest text, input_previous_digest text, input_client_id text,
    input_redirect_uri text, input_code_challenge text, input_state text,
    input_device_label text, input_replaces text
) RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    checked_at timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF input_request_digest IS NULL OR input_request_digest !~ '^[0-9a-f]{64}$'
       OR (input_previous_digest IS NOT NULL AND input_previous_digest !~ '^[0-9a-f]{64}$')
       OR input_client_id IS NULL OR input_client_id !~ '^[a-z0-9-]{1,64}$'
       OR input_redirect_uri IS NULL OR pg_catalog.char_length(input_redirect_uri) NOT BETWEEN 1 AND 256
       OR input_code_challenge IS NULL OR input_code_challenge !~ '^[A-Za-z0-9_-]{43}$'
       OR input_state IS NULL OR input_state !~ '^[!-~]+$' OR pg_catalog.char_length(input_state) > 256
       OR input_device_label IS NULL OR input_device_label = ''
       OR pg_catalog.char_length(input_device_label) > 64
       OR (input_replaces IS NOT NULL AND input_replaces !~ '^[A-Za-z0-9_-]{22}$') THEN
        RETURN NULL;
    END IF;
    -- Serializes the cap check with concurrent starts.
    PERFORM pg_catalog.pg_advisory_xact_lock(901413, 1);
    DELETE FROM public.mobile_authorization_requests r WHERE r.expires_at <= checked_at;
    IF input_previous_digest IS NOT NULL THEN
        DELETE FROM public.mobile_authorization_requests r
        WHERE r.request_digest = input_previous_digest;
    END IF;
    IF (SELECT pg_catalog.count(*) FROM public.mobile_authorization_requests) >= 1000 THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.mobile_authorization_requests
        (request_digest, client_id, redirect_uri, code_challenge, state, device_label,
         replaces, created_at, expires_at)
    VALUES (input_request_digest, input_client_id, input_redirect_uri, input_code_challenge,
            input_state, input_device_label, input_replaces, checked_at,
            checked_at + interval '10 minutes');
    RETURN checked_at + interval '10 minutes';
END
$body$;

-- The live request's expiry, or NULL. Lets a login return to the confirm page.
CREATE FUNCTION public.mobile_authorization_live(input_request_digest text)
RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    RETURN (SELECT r.expires_at FROM public.mobile_authorization_requests r
            WHERE r.request_digest = input_request_digest
              AND r.expires_at > pg_catalog.clock_timestamp());
END
$body$;

-- What the confirm page shows: the request, the account's active grant
-- count and, when `replaces` names one of the account's active grants, that
-- grant. A grant is active while it is not revoked, not idle-expired (60
-- days after the later of creation and last use) and issued at the account's
-- current auth_version, as in api_grant_create. No row when the request is
-- missing or expired or the account is unavailable.
CREATE FUNCTION public.mobile_authorization_review(
    input_request_digest text, input_account_id bigint, input_auth_version bigint
) RETURNS TABLE (
    result_client_id text, result_redirect_uri text, result_device_label text,
    result_expires_at timestamptz, result_active_grants bigint,
    result_replaces text, result_replaces_label text,
    result_replaces_created_at timestamptz, result_replaces_last_used_at timestamptz
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    request_row public.mobile_authorization_requests%ROWTYPE;
    checked_at timestamptz := pg_catalog.clock_timestamp();
    replaced public.api_grants%ROWTYPE;
    active_count bigint;
BEGIN
    SELECT * INTO request_row FROM public.mobile_authorization_requests r
    WHERE r.request_digest = input_request_digest AND r.expires_at > checked_at;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM public.accounts a WHERE a.id = input_account_id
          AND a.is_enabled AND a.auth_version = input_auth_version
    ) THEN
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO active_count FROM public.api_grants g
    WHERE g.account_id = input_account_id AND g.revoked_at IS NULL
      AND g.issued_auth_version = input_auth_version
      AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    IF request_row.replaces IS NOT NULL THEN
        SELECT * INTO replaced FROM public.api_grants g
        WHERE g.account_id = input_account_id AND g.public_id = request_row.replaces
          AND g.revoked_at IS NULL AND g.issued_auth_version = input_auth_version
          AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    END IF;
    RETURN QUERY SELECT request_row.client_id, request_row.redirect_uri, request_row.device_label,
        request_row.expires_at, active_count, replaced.public_id, replaced.device_label,
        replaced.created_at, replaced.last_used_at;
END
$body$;

-- Approval ends the pending request and, below the active-grant limit, issues
-- a code bound to the request, the account, its auth_version, the challenge
-- and the honored `replaces`. Results: 'approved', 'grant_limit_reached'
-- (the request still ends, no code), or 'unavailable' (missing or expired
-- request, or the account changed), each with the request's callback values.
CREATE FUNCTION public.mobile_authorization_approve(
    input_request_digest text, input_account_id bigint, input_auth_version bigint,
    input_code_digest text
) RETURNS TABLE (result text, result_redirect_uri text, result_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    request_row public.mobile_authorization_requests%ROWTYPE;
    checked_at timestamptz := pg_catalog.clock_timestamp();
    honored text;
    active_count bigint;
BEGIN
    IF input_code_digest IS NULL OR input_code_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::text, NULL::text;
        RETURN;
    END IF;
    DELETE FROM public.mobile_authorization_requests r
    WHERE r.request_digest = input_request_digest
    RETURNING r.* INTO request_row;
    IF NOT FOUND OR request_row.expires_at <= checked_at THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::text, NULL::text;
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.accounts a WHERE a.id = input_account_id
          AND a.is_enabled AND a.auth_version = input_auth_version
    ) THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::text, NULL::text;
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO active_count FROM public.api_grants g
    WHERE g.account_id = input_account_id AND g.revoked_at IS NULL
      AND g.issued_auth_version = input_auth_version
      AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    IF request_row.replaces IS NOT NULL THEN
        SELECT g.public_id INTO honored FROM public.api_grants g
        WHERE g.account_id = input_account_id AND g.public_id = request_row.replaces
          AND g.revoked_at IS NULL AND g.issued_auth_version = input_auth_version
          AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    END IF;
    IF active_count - (CASE WHEN honored IS NULL THEN 0 ELSE 1 END) >= 10 THEN
        RETURN QUERY SELECT 'grant_limit_reached'::text, request_row.redirect_uri, request_row.state;
        RETURN;
    END IF;
    DELETE FROM public.mobile_authorization_codes c WHERE c.expires_at <= checked_at;
    INSERT INTO public.mobile_authorization_codes
        (code_digest, account_id, issued_auth_version, request_digest, client_id,
         code_challenge, device_label, replaces, created_at, expires_at)
    VALUES (input_code_digest, input_account_id, input_auth_version, request_row.request_digest,
            request_row.client_id, request_row.code_challenge, request_row.device_label, honored,
            checked_at, checked_at + interval '60 seconds');
    RETURN QUERY SELECT 'approved'::text, request_row.redirect_uri, request_row.state;
END
$body$;

-- Denial ends the pending request. The result is 'grant_limit_reached' when
-- the account could not have approved it, else 'access_denied'; no row when
-- the request is missing or expired.
CREATE FUNCTION public.mobile_authorization_deny(
    input_request_digest text, input_account_id bigint, input_auth_version bigint
) RETURNS TABLE (result text, result_redirect_uri text, result_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    request_row public.mobile_authorization_requests%ROWTYPE;
    checked_at timestamptz := pg_catalog.clock_timestamp();
    honored text;
    active_count bigint;
BEGIN
    DELETE FROM public.mobile_authorization_requests r
    WHERE r.request_digest = input_request_digest
    RETURNING r.* INTO request_row;
    IF NOT FOUND OR request_row.expires_at <= checked_at THEN
        RETURN;
    END IF;
    SELECT pg_catalog.count(*) INTO active_count FROM public.api_grants g
    WHERE g.account_id = input_account_id AND g.revoked_at IS NULL
      AND g.issued_auth_version = input_auth_version
      AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    IF request_row.replaces IS NOT NULL THEN
        SELECT g.public_id INTO honored FROM public.api_grants g
        WHERE g.account_id = input_account_id AND g.public_id = request_row.replaces
          AND g.revoked_at IS NULL AND g.issued_auth_version = input_auth_version
          AND GREATEST(g.created_at, COALESCE(g.last_used_at, g.created_at)) > checked_at - interval '60 days';
    END IF;
    RETURN QUERY SELECT
        CASE WHEN active_count - (CASE WHEN honored IS NULL THEN 0 ELSE 1 END) >= 10
             THEN 'grant_limit_reached' ELSE 'access_denied' END,
        request_row.redirect_uri, request_row.state;
END
$body$;

-- Consumes a live code under its row lock: a concurrent exchange of the same
-- code waits here and then finds it consumed. Returns the bound values, or no
-- row for a consumed, expired or unknown code. The caller's transaction
-- decides whether the consumption commits.
CREATE FUNCTION public.mobile_authorization_code_consume(input_code_digest text)
RETURNS TABLE (
    result_account_id bigint, result_auth_version bigint, result_client_id text,
    result_code_challenge text, result_device_label text, result_replaces text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF input_code_digest IS NULL OR input_code_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.mobile_authorization_codes c SET consumed_at = pg_catalog.clock_timestamp()
    WHERE c.code_digest = input_code_digest AND c.consumed_at IS NULL
      AND c.expires_at > pg_catalog.clock_timestamp()
    RETURNING c.account_id, c.issued_auth_version, c.client_id, c.code_challenge,
              c.device_label, c.replaces;
END
$body$;

REVOKE ALL ON FUNCTION public.mobile_authorization_start(text,text,text,text,text,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mobile_authorization_live(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mobile_authorization_review(text,bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mobile_authorization_approve(text,bigint,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mobile_authorization_deny(text,bigint,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mobile_authorization_code_consume(text) FROM PUBLIC;

DO $permissions$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        ALTER TABLE public.mobile_authorization_requests OWNER TO odograph_migrate;
        ALTER TABLE public.mobile_authorization_codes OWNER TO odograph_migrate;
        REVOKE ALL ON public.mobile_authorization_requests, public.mobile_authorization_codes
            FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        CREATE POLICY account_isolation ON public.mobile_authorization_codes FOR ALL TO odograph_runtime
            USING (account_id = NULLIF(current_setting('app.account_id', true), '')::bigint)
            WITH CHECK (account_id = NULLIF(current_setting('app.account_id', true), '')::bigint);
        CREATE POLICY migration_writer ON public.mobile_authorization_codes FOR ALL TO odograph_migrate
            USING (true) WITH CHECK (true);
        ALTER FUNCTION public.mobile_authorization_start(text,text,text,text,text,text,text,text) OWNER TO odograph_migrate;
        ALTER FUNCTION public.mobile_authorization_live(text) OWNER TO odograph_migrate;
        ALTER FUNCTION public.mobile_authorization_review(text,bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.mobile_authorization_approve(text,bigint,bigint,text) OWNER TO odograph_migrate;
        ALTER FUNCTION public.mobile_authorization_deny(text,bigint,bigint) OWNER TO odograph_migrate;
        ALTER FUNCTION public.mobile_authorization_code_consume(text) OWNER TO odograph_migrate;
        REVOKE ALL ON FUNCTION public.mobile_authorization_start(text,text,text,text,text,text,text,text),
            public.mobile_authorization_live(text), public.mobile_authorization_review(text,bigint,bigint),
            public.mobile_authorization_approve(text,bigint,bigint,text),
            public.mobile_authorization_deny(text,bigint,bigint),
            public.mobile_authorization_code_consume(text)
            FROM PUBLIC, odograph_control, odograph_runtime, odograph_bootstrap;
        GRANT EXECUTE ON FUNCTION public.mobile_authorization_start(text,text,text,text,text,text,text,text),
            public.mobile_authorization_live(text), public.mobile_authorization_review(text,bigint,bigint),
            public.mobile_authorization_approve(text,bigint,bigint,text),
            public.mobile_authorization_deny(text,bigint,bigint),
            public.mobile_authorization_code_consume(text)
            TO odograph_control;
    END IF;
END
$permissions$;
