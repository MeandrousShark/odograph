from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.validation import parse_finite_number

log = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

ENV_PREFIX = "MILEAGE_RATE_"


@dataclass(frozen=True)
class YearRate:
    """A year's IRS standard mileage rate. Most years are a single flat rate
    (`rate_per_mi`, both split fields None). A year with a mid-year IRS change
    (e.g. 2022) also sets `rate_h2_per_mi` and the `h2_start_month` it takes
    effect from, so a trip is priced by the rate in force on its own date.
    """
    rate_per_mi: float
    rate_h2_per_mi: float | None = None   # second-half rate; None = no mid-year change
    h2_start_month: int | None = None     # month (1-12) the second-half rate starts

    def rate(self, month: int) -> float:
        if (
            self.rate_h2_per_mi is not None
            and self.h2_start_month is not None
            and month >= self.h2_start_month
        ):
            return self.rate_h2_per_mi
        return self.rate_per_mi


def rate_for(rates: dict[int, YearRate], year: int, month: int = 1) -> float | None:
    """The rate in force for `year`/`month`. Exact year if published (honoring
    any mid-year split), else the most recent earlier year's *latest* rate
    (covers January before that year's IRS notice -- if the prior year had a
    mid-year bump, its second-half rate is the best proxy), else None if no
    rate has ever been published at or before `year`.
    """
    yr = rates.get(year)
    if yr is not None:
        return yr.rate(month)
    earlier = [y for y in rates if y < year]
    if not earlier:
        return None
    return rates[max(earlier)].rate(12)


def deduction(
    distance_m: float, year: int, rates: dict[int, YearRate], month: int = 1
) -> float | None:
    rate = rate_for(rates, year, month)
    if rate is None:
        return None
    return (distance_m / METERS_PER_MILE) * rate


async def load_rates(conn) -> dict[int, YearRate]:
    """Rates from `mileage_rates`, overridden per-year by `MILEAGE_RATE_<YEAR>`
    env vars (e.g. `MILEAGE_RATE_2026=0.725`) without touching the DB. An env
    override sets a single flat rate for that year (no mid-year split).
    """
    cur = await conn.execute(
        "SELECT year, rate_per_mi, rate_h2_per_mi, h2_start_month FROM mileage_rates"
    )
    rates: dict[int, YearRate] = {}
    for year, r1, r2, m in await cur.fetchall():
        rates[year] = YearRate(
            rate_per_mi=float(r1),
            rate_h2_per_mi=float(r2) if r2 is not None else None,
            h2_start_month=int(m) if m is not None else None,
        )
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        try:
            year = int(key[len(ENV_PREFIX):])
            numeric = float(value)
        except ValueError:
            continue
        # This is config load with no request to reject: an override that's
        # non-finite (nan, inf) or non-positive is logged and skipped so any
        # rate already on file for the year still applies, rather than
        # crashing the process or installing a non-finite rate.
        rate = parse_finite_number(numeric)
        if rate is None or rate <= 0:
            log.warning("ignoring invalid %s%s override: %r", ENV_PREFIX, year, value)
            continue
        rates[year] = YearRate(rate_per_mi=rate)
    return rates
