-- Migration 013 normalized the cache that existed at its deployment instant,
-- but the live Geoapify response shape exposed a parser gap immediately after
-- it was applied and allowed a new row to regain this exact suffix. Migration
-- history is immutable, so repeat the idempotent data cleanup after fixing the
-- parser. Similar and foreign strings remain untouched.
UPDATE geocode_cache
SET address = LEFT(
    address,
    LENGTH(address) - LENGTH(', United States of America')
)
WHERE RIGHT(address, LENGTH(', United States of America'))
      = ', United States of America';
