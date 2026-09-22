CREATE OR REPLACE FUNCTION public.assert_tracking_credential(
    credential_public_id text,
    credential_generation bigint,
    requested_device_id bigint,
    requested_device_generation bigint,
    requested_legacy_label text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    owner_id bigint := NULLIF(current_setting('app.account_id', true), '')::bigint;
    credential record;
    device record;
    alias_device_id bigint;
BEGIN
    SELECT account_id, tracking_device_id, kind, generation, revoked_at
    INTO credential FROM public.ingest_credentials
    WHERE public_id = credential_public_id AND account_id = owner_id FOR SHARE;
    IF NOT FOUND OR credential.revoked_at IS NOT NULL
       OR credential.generation IS DISTINCT FROM credential_generation THEN
        RAISE EXCEPTION 'Tracking credential is unavailable' USING ERRCODE = '42501';
    END IF;
    IF credential.kind = 'device' AND
       requested_device_id IS DISTINCT FROM credential.tracking_device_id THEN
        RAISE EXCEPTION 'Tracking credential is unavailable' USING ERRCODE = '42501';
    END IF;
    IF requested_device_id IS NULL THEN
        IF requested_device_generation IS NOT NULL OR requested_legacy_label IS NOT NULL THEN
            RAISE EXCEPTION 'Tracking credential is unavailable' USING ERRCODE = '42501';
        END IF;
        RETURN;
    END IF;
    IF credential.kind = 'legacy' THEN
        SELECT tracking_device_id INTO alias_device_id
        FROM public.tracking_device_aliases
        WHERE account_id = owner_id AND original_label = requested_legacy_label AND enabled
        FOR SHARE;
        IF NOT FOUND OR alias_device_id <> requested_device_id THEN
            RAISE EXCEPTION 'Tracking alias is unavailable' USING ERRCODE = '42501';
        END IF;
    ELSIF requested_legacy_label IS NOT NULL THEN
        RAISE EXCEPTION 'Tracking credential is unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT enabled, revoked_at, generation INTO device FROM public.tracking_devices
    WHERE account_id = owner_id AND id = requested_device_id FOR SHARE;
    IF NOT FOUND OR NOT device.enabled OR device.revoked_at IS NOT NULL
       OR device.generation IS DISTINCT FROM requested_device_generation THEN
        RAISE EXCEPTION 'Tracking device is unavailable' USING ERRCODE = '42501';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION public.assert_tracking_credential(text, bigint, bigint, bigint, text)
    FROM PUBLIC;
