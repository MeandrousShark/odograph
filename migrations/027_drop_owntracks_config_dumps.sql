-- OwnTracks "dump" and "configuration" messages carry the tracker's full
-- configuration, including its username, password and URL. Ingest no
-- longer stores them (only location, transition, waypoint and waypoints
-- reach raw_messages), but earlier releases stored every authenticated
-- payload verbatim. Nothing reads these rows, so delete them rather than
-- leave plaintext credentials to reach later backups.
DELETE FROM raw_messages WHERE payload->>'_type' IN ('dump', 'configuration');
