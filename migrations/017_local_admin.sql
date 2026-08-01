-- Single-row local administrator, bootstrapped and reset via the
-- token-gated /setup page (app/auth.py) rather than a full multi-user
-- model -- multi-user support gets its own table later. CHECK (id = 1) as
-- the primary key makes "there can only ever be one row" a database
-- invariant instead of a code convention: a second INSERT can never
-- silently create a second administrator, even in the presence of a bug.
CREATE TABLE local_admin (
    id                  smallint PRIMARY KEY CHECK (id = 1),
    email               text NOT NULL,
    password_hash       text NOT NULL,
    -- SHA-256 (not scrypt) of the setup token most recently used
    -- successfully: tokens are high-entropy and machine-generated, unlike
    -- passwords, so a slow hash buys nothing here, and a fast one is all
    -- that's needed to auto-consume a token on use.
    consumed_token_hash text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
