-- An optional display name for the account header and initials. It is
-- presentation metadata only: login, OIDC identity matching and every
-- authorization decision keep using the login email and account ID.
ALTER TABLE public.accounts ADD COLUMN display_name text;

-- The same already-trimmed convention as 025_manual_trip_labels.sql. The
-- application runs the full Unicode trim and character checks; this only
-- catches a direct write that skipped them. char_length, not octet_length,
-- so a name in a multi-byte script is not penalized for its encoded size.
ALTER TABLE public.accounts
    ADD CONSTRAINT accounts_display_name_trimmed_nonblank
        CHECK (display_name IS NULL OR (display_name = btrim(display_name) AND display_name <> '')),
    ADD CONSTRAINT accounts_display_name_length
        CHECK (display_name IS NULL OR char_length(display_name) <= 100);

DO $$
BEGIN
    IF to_regclass('odograph_service.managed_role_state') IS NOT NULL THEN
        GRANT UPDATE (display_name) ON public.accounts TO odograph_control;
    END IF;
END $$;
