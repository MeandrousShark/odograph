"""The upgrade migrations and current role contract install the same functions."""
from __future__ import annotations

import re
from pathlib import Path

from app.application_roles import INVITATION_FUNCTIONS, SQL_DIR
from app.db import INVITATION_EMAIL_LOCK_CLASS_ID


def test_migrations_match_current_invitation_functions():
    current = (SQL_DIR / "member_invitations.sql").read_text()
    for function in INVITATION_FUNCTIONS:
        if "redeem_oidc" in function:
            migration_name = "032_oidc_member_invitation.sql"
        elif "redeem_member" in function:
            migration_name = "029_invitations.sql"
        else:
            migration_name = "034_admin_invitations.sql"
        migration = (SQL_DIR.parent.parent / "migrations" / migration_name).read_text()
        name = function.split("(", 1)[0]
        pattern = rf"CREATE (?:OR REPLACE )?FUNCTION {re.escape(name)}\(.*?AS\s+(\$body\$)(.*?)\1"
        migration_body = re.search(pattern, migration, re.S | re.I).group(2)
        current_body = re.search(pattern, current, re.S | re.I).group(2)
        assert migration_body == current_body


def test_invitation_email_lock_uses_registered_namespace():
    migrations = [
        (SQL_DIR.parent.parent / "migrations" / name).read_text()
        for name in ("029_invitations.sql", "032_oidc_member_invitation.sql",
                     "034_admin_invitations.sql")
    ]
    current = (SQL_DIR / "member_invitations.sql").read_text()
    lock_call = f"pg_advisory_xact_lock({INVITATION_EMAIL_LOCK_CLASS_ID},"
    assert [migration.count(lock_call) for migration in migrations] == [2, 1, 4]
    assert current.count(lock_call) == 6
