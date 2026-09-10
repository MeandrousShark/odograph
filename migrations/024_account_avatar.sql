-- An account avatar lives in the database rather than on disk or in object
-- storage: this is a single-account instance, and keeping the image inside
-- the same backup/restore boundary as everything else avoids a second thing
-- to provision, mount, and restore in sync with the database.
ALTER TABLE accounts
    ADD COLUMN avatar_bytes bytea,
    ADD COLUMN avatar_mime text CHECK (
        avatar_mime IS NULL OR avatar_mime IN ('image/png', 'image/jpeg', 'image/webp')
    ),
    ADD COLUMN avatar_updated_at timestamptz;

-- All three columns are set or cleared together; a bytes-with-no-mime (or
-- any other partial) row would mean the serving route has to guess.
ALTER TABLE accounts
    ADD CONSTRAINT accounts_avatar_columns_pair
    CHECK (
        (avatar_bytes IS NULL) = (avatar_mime IS NULL)
        AND (avatar_bytes IS NULL) = (avatar_updated_at IS NULL)
    );
