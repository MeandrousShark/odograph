-- migration-class: additive
-- A client-chosen key for retrying a create safely. The key lives on the
-- record it created, scoped to the account and the table, together with a
-- SHA-256 digest of the validated request, so a retry with the same key and
-- request finds the existing record and a different request with that key
-- is refused. Web creates leave both columns null. Neither column is part
-- of the storage charge or the portable bundle.
ALTER TABLE public.trips
    ADD COLUMN idempotency_key uuid,
    ADD COLUMN idempotency_digest bytea,
    ADD CONSTRAINT trips_idempotency_pair
        CHECK ((idempotency_key IS NULL) = (idempotency_digest IS NULL)),
    ADD CONSTRAINT trips_idempotency_digest_length
        CHECK (idempotency_digest IS NULL OR octet_length(idempotency_digest) = 32);

CREATE UNIQUE INDEX trips_idempotency_key_idx ON public.trips (account_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE public.expenses
    ADD COLUMN idempotency_key uuid,
    ADD COLUMN idempotency_digest bytea,
    ADD CONSTRAINT expenses_idempotency_pair
        CHECK ((idempotency_key IS NULL) = (idempotency_digest IS NULL)),
    ADD CONSTRAINT expenses_idempotency_digest_length
        CHECK (idempotency_digest IS NULL OR octet_length(idempotency_digest) = 32);

CREATE UNIQUE INDEX expenses_idempotency_key_idx ON public.expenses (account_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
