-- Geoapify now omits the domestic country suffix before caching new US
-- addresses. Normalize cache rows written before that behavior landed so the
-- full-address tooltip, detail views, and exports do not retain the old noise.
-- RIGHT's exact comparison preserves foreign countries and similar strings;
-- rerunning this statement has no further effect.
UPDATE geocode_cache
SET address = LEFT(
    address,
    LENGTH(address) - LENGTH(', United States of America')
)
WHERE RIGHT(address, LENGTH(', United States of America'))
      = ', United States of America';
