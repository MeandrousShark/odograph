from __future__ import annotations

from dataclasses import dataclass

from app.account_context import account_id

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
    """Load the account's effective rates, including any one-time legacy import."""
    cur = await conn.execute(
        "SELECT year, rate_per_mi, rate_h2_per_mi, h2_start_month FROM mileage_rates "
        "WHERE account_id = %s",
        (account_id(conn),),
    )
    rates: dict[int, YearRate] = {}
    for year, r1, r2, m in await cur.fetchall():
        rates[year] = YearRate(
            rate_per_mi=float(r1),
            rate_h2_per_mi=float(r2) if r2 is not None else None,
            h2_start_month=int(m) if m is not None else None,
        )
    return rates
