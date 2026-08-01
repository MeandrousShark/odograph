"""Unit tests for the local-admin password/token hashing helpers."""
from __future__ import annotations

import base64
import hashlib

from app import local_auth
from app.local_auth import hash_password, sha256_hex, verify_password


def test_hash_password_round_trips_through_verify():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_verify_password_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", stored)


def test_hash_password_uses_a_fresh_salt_each_time():
    # Same input password, different stored strings -- proves the salt
    # isn't fixed/reused, which would make identical passwords produce
    # identical hashes (a rainbow-table risk).
    a = hash_password("same password")
    b = hash_password("same password")
    assert a != b
    assert verify_password("same password", a)
    assert verify_password("same password", b)


def test_verify_password_tolerates_older_scrypt_parameters():
    # Parameters are read back out of the stored string, not the module's
    # current SCRYPT_N/R/P constants -- a hash created under smaller
    # (older/weaker) parameters must still verify after those constants are
    # raised later, since there's no migration step that rewrites existing
    # stored hashes.
    old_n, old_r, old_p = 2**10, 4, 1
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(
        b"legacy password", salt=salt, n=old_n, r=old_r, p=old_p, dklen=64,
    )
    stored = "$".join((
        "scrypt", str(old_n), str(old_r), str(old_p),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))
    assert stored.split("$")[1] != str(local_auth.SCRYPT_N)
    assert verify_password("legacy password", stored)
    assert not verify_password("wrong password", stored)


def test_verify_password_rejects_malformed_stored_hash_without_raising():
    assert not verify_password("anything", "not-a-valid-hash")
    assert not verify_password("anything", "scrypt$notanint$8$1$c2FsdA==$ZGlnZXN0")
    assert not verify_password("anything", "bcrypt$10$abc$def")


def test_sha256_hex_is_deterministic_and_distinguishes_input():
    assert sha256_hex("a-token") == sha256_hex("a-token")
    assert sha256_hex("a-token") != sha256_hex("a-different-token")
    assert sha256_hex("a-token") == hashlib.sha256(b"a-token").hexdigest()
