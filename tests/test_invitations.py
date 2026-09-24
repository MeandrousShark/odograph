"""The upgrade migration and current role contract install the same functions."""
from __future__ import annotations

import re
from pathlib import Path

from app.application_roles import INVITATION_FUNCTIONS, SQL_DIR
from app.db import INVITATION_EMAIL_LOCK_CLASS_ID


def test_migration_029_matches_current_invitation_functions():
    migration = (SQL_DIR.parent.parent / "migrations" / "029_invitations.sql").read_text()
    current = (SQL_DIR / "member_invitations.sql").read_text()
    for function in INVITATION_FUNCTIONS:
        name = function.split("(", 1)[0]
        pattern = rf"CREATE (?:OR REPLACE )?FUNCTION {re.escape(name)}\(.*?AS\s+(\$body\$)(.*?)\1"
        migration_body = re.search(pattern, migration, re.S | re.I).group(2)
        current_body = re.search(pattern, current, re.S | re.I).group(2)
        assert migration_body == current_body


def test_invitation_email_lock_uses_registered_namespace():
    migration = (SQL_DIR.parent.parent / "migrations" / "029_invitations.sql").read_text()
    current = (SQL_DIR / "member_invitations.sql").read_text()
    lock_call = f"pg_advisory_xact_lock({INVITATION_EMAIL_LOCK_CLASS_ID},"
    assert migration.count(lock_call) == 2
    assert current.count(lock_call) == 2
