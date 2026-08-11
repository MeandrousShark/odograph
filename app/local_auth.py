"""Password hashing for local account credentials."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

# Interactive login, not a background job: n=2**15 keeps a single verify
# under roughly 100ms on modest hardware while still costing real work per
# guess. r=8/p=1 are the standard companions RFC 7914 pairs with N. Stored
# per-hash (below) rather than fixed globally, so raising these later
# doesn't invalidate hashes created under the old parameters.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
DKLEN = 64


def _maxmem(n: int, r: int, p: int) -> int:
    # OpenSSL's hashlib.scrypt refuses to run above a 32MiB working-set
    # default (`maxmem=0`), and SCRYPT_N/R/P above exceed it -- confirmed
    # experimentally that OpenSSL's actual working set runs closer to
    # 256*N*r*p bytes (not the textbook 128*N*r*p) once its V/B scratch
    # buffers are both counted, so the multiplier here is doubled past that
    # textbook figure rather than the exact requirement, to leave headroom.
    # An explicit maxmem avoids a "memory limit exceeded" ValueError on the
    # parameters this module picks, and keeps verify_password working for
    # any stored n/r/p (including future-raised ones) without a matching
    # bump here.
    return 256 * n * r * p + 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R, SCRYPT_P),
    )
    return "$".join((
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches `stored`. Reads scrypt parameters back out
    of `stored` (not the module constants above) so a hash created under
    older parameters still verifies after SCRYPT_N/R/P are raised.
    """
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        n, r, p = int(n), int(r), int(p)
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
            maxmem=_maxmem(n, r, p),
        )
    except (ValueError, OverflowError, binascii.Error):
        # Covers both a genuinely malformed stored value (bad base64, wrong
        # field count) and a corrupt/absurd n/r/p that would otherwise raise
        # out of hashlib.scrypt -- either way, "doesn't verify", not a 500.
        return False
    return hmac.compare_digest(candidate, expected)
