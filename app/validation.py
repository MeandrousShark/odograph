"""Shared input validation. `parse_finite_number` is used both by
`app/portable/normalize.py` (a bundle value pulled from arbitrary uploaded
JSON) and by `app/ui/`'s form handlers plus `app/rates.py`'s env-var override
(a value FastAPI/pydantic or `float()` has already turned into a Python float
but never checked for NaN/Infinity), so the same non-finite/range rule applies
everywhere a bare float reaches the database or a computation.
"""
from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote


def parse_finite_number(
    value: Any, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    """Rejects NaN/Infinity the way `_parse_amount` in `app/portable/normalize.py`
    rejects a non-finite `Decimal`: `json.loads` accepts the bare
    `NaN`/`Infinity` tokens JSON itself doesn't allow, and a plain `< 0`/`<= 0`
    bound check lets a non-finite value straight through (Postgres even sorts
    NaN as greater than every real number, so a `CHECK (x > 0)` column doesn't
    catch it either). `minimum`/`maximum` are an inclusive floor/ceiling:
    distance_m and odometer_m use only `minimum=0`, while place lat/lon use
    both to enforce the `[-180 -90, 180 90]` range themselves, since the
    geography cast that stores them does not: it silently coerces an
    out-of-range coordinate rather than raising, so an out-of-range value
    must be caught here or it reaches the database wrong instead of refused.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


# ntfy's own topic format. A topic is one URL path segment on the server.
NTFY_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
NTFY_TOPIC_HINT = "use 1 to 64 letters, digits, hyphens or underscores"


def is_valid_ntfy_topic(topic: Any) -> bool:
    return isinstance(topic, str) and NTFY_TOPIC_RE.fullmatch(topic) is not None


def ntfy_topic_url(server_url: str, topic: str) -> str:
    return server_url.rstrip("/") + "/" + quote(topic, safe="")
