"""Administrator storage limit editor functions and their role contract."""
import asyncio
import os
import shutil
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import errors

import app.db as db_module
from app import application_roles
from app.application_roles import prepare_application_roles
from app.db import MIGRATIONS_DIR, make_pool, run_migrations
from app.role_setup import RoleSetupError
from app.storage import is_storage_capacity_error, storage_status
from app.storage_limits import (
    MIN_CUSTOM_LIMIT_BYTES,
    StorageLimitUnavailable,
    StorageLimitUnfunded,
    list_account_storage,
    set_account_storage_limit,
)
from conftest import (add_test_account, drop_and_recreate_schema, reset_account_db,
                      restore_test_schema, restricted_role_pools)


TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="requires disposable TEST_DATABASE_URL")
GIB = 1024 ** 3
MIB = 1024 ** 2
SETTER = "public.storage_set_account_grant(bigint,bigint,bigint,text,bigint)"
READER = "public.storage_list_account_grants(bigint,bigint)"


def _run(body):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            admin_pool = await reset_account_db(raw)
            pools = await restricted_role_pools(raw)
            await body(raw, pools, admin_pool.principal.account_id)
        finally:
            await raw.close()
    asyncio.run(scenario())


def _limits(account=None, raw=None, enhancement=None, budget=12 * GIB, reserve=2 * GIB):
    return SimpleNamespace(
        storage_account_limit_bytes=account, storage_raw_limit_bytes=raw,
        storage_enhancement_limit_bytes=enhancement,
        storage_instance_budget_bytes=budget, storage_instance_reserve_bytes=reserve,
    )


async def _configure(conn, account, raw, enhancement, budget=12 * GIB, reserve=2 * GIB):
    await conn.execute("SELECT public.storage_configure_limits(%s,%s,%s,%s,%s)",
                       (account, raw, enhancement, budget, reserve))


async def _grant(conn, owner):
    return await (await conn.execute(
        "SELECT limit_mode,account_limit_bytes,raw_limit_bytes,enhancement_limit_bytes "
        "FROM storage_grants WHERE account_id=%s", (owner,))).fetchone()


async def _set(conn, actor, target, mode, size=None, *, version=1):
    async with conn.transaction():
        return (await (await conn.execute(
            "SELECT public.storage_set_account_grant(%s,%s,%s,%s,%s)",
            (actor, version, target, mode, size))).fetchone())[0]


async def _rows(conn, actor, version=1):
    async with conn.transaction():
        return await (await conn.execute(
            "SELECT * FROM public.storage_list_account_grants(%s,%s)", (actor, version))).fetchall()


def test_editor_functions_require_a_live_administrator_on_the_control_role():
    async def body(raw, pools, admin):
        await add_test_account(raw, 77)
        await add_test_account(raw, 78)
        async with raw.connection() as conn:
            await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin,is_enabled) "
                               "VALUES(90,'disabled-admin@example.invalid','unused',true,false)")
            assert await (await conn.execute(
                "SELECT pg_get_userbyid(proowner),prosecdef FROM pg_proc "
                "WHERE oid IN (%s::regprocedure,%s::regprocedure)", (SETTER, READER))).fetchall() == [
                ("odograph_migrate", True)] * 2
            for role, allowed in (("odograph_control", True), ("odograph_runtime", False),
                                  ("odograph_bootstrap", False), ("public", False)):
                for function in (SETTER, READER):
                    if role == "public":
                        query = ("SELECT COALESCE(bool_or(a.privilege_type='EXECUTE' AND a.grantee=0),false) FROM pg_proc p, "
                                 "aclexplode(p.proacl) a WHERE p.oid=%s::regprocedure")
                        params = (function,)
                    else:
                        query = "SELECT has_function_privilege(%s,%s,'EXECUTE')"
                        params = (role, function)
                    assert (await (await conn.execute(query, params)).fetchone())[0] is allowed, (role, function)
        async with pools.runtime.connection() as conn:
            for statement, params in (
                ("SELECT public.storage_set_account_grant(%s,1,77,'unlimited',NULL)", (admin,)),
                ("SELECT * FROM public.storage_list_account_grants(%s,1)", (admin,)),
            ):
                with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
                    async with conn.transaction():
                        await conn.execute(statement, params)
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute("SELECT * FROM storage_grants")
            for actor, version in ((78, 1), (90, 1), (admin, 2), (None, 1), (admin, None)):
                with pytest.raises(errors.InsufficientPrivilege, match="unavailable"):
                    await _set(conn, actor, 77, "unlimited", version=version)
                with pytest.raises(errors.InsufficientPrivilege, match="unavailable"):
                    await _rows(conn, actor, version)
            with pytest.raises(errors.InsufficientPrivilege, match="unavailable"):
                await _set(conn, admin, 999, "unlimited")
            assert await _set(conn, admin, 77, "unlimited") == "changed"
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("unlimited", None, None, None)
            await conn.execute("UPDATE accounts SET auth_version=2 WHERE id=%s", (admin,))
        async with pools.control.connection() as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                await _set(conn, admin, 77, "default", version=1)
            assert await _set(conn, admin, 77, "default", version=2) == "changed"
    _run(body)


def test_modes_follow_policy_and_clamp_custom_sub_limits():
    async def body(raw, pools, admin):
        await add_test_account(raw, 77)
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", 200 * MIB) == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("custom", 200 * MIB, 200 * MIB, 128 * MIB)
            assert await _set(conn, admin, 77, "custom", 64 * MIB) == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("custom", 64 * MIB, 64 * MIB, 64 * MIB)
            assert await _set(conn, admin, 77, "custom", 64 * MIB) == "unchanged"
            assert await _set(conn, admin, 77, "custom", 5 * GIB) == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("custom", 5 * GIB, 256 * MIB, 128 * MIB)
            assert await _set(conn, admin, 77, "unlimited") == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("unlimited", None, None, None)
            assert await _set(conn, admin, 77, "unlimited") == "unchanged"
            assert await _set(conn, admin, 77, "default") == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("default", 2 * GIB, 256 * MIB, 128 * MIB)
            assert await _set(conn, admin, 77, "default") == "unchanged"
        async with raw.connection() as conn:
            await _configure(conn, None, None, None)
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", GIB) == "changed"
            async with raw.connection() as check:
                # A NULL policy default stays NULL rather than taking the custom limit.
                assert await _grant(check, 77) == ("custom", GIB, None, None)
            assert await _set(conn, admin, 77, "default") == "changed"
            async with raw.connection() as check:
                assert await _grant(check, 77) == ("default", None, None, None)
    _run(body)


def test_invalid_input_and_administrator_targets_are_refused():
    async def body(raw, pools, admin):
        await add_test_account(raw, 77)
        await add_test_account(raw, 90, admin=True)
        async with pools.control.connection() as conn:
            for mode, size in (("custom", None), ("custom", 0), ("custom", -1),
                               ("custom", MIN_CUSTOM_LIMIT_BYTES - 1), ("default", GIB),
                               ("unlimited", GIB), ("bogus", None), (None, None)):
                with pytest.raises(errors.InvalidParameterValue, match="invalid storage limit"):
                    await _set(conn, admin, 77, mode, size)
            assert await _set(conn, admin, 77, "custom", MIN_CUSTOM_LIMIT_BYTES) == "changed"
            for target in (90, admin):
                for mode, size in (("custom", GIB), ("default", None)):
                    with pytest.raises(errors.InvalidParameterValue, match="always unlimited"):
                        await _set(conn, admin, target, mode, size)
                assert await _set(conn, admin, target, "unlimited") == "unchanged"
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == (
                "custom", MIN_CUSTOM_LIMIT_BYTES, MIN_CUSTOM_LIMIT_BYTES, MIN_CUSTOM_LIMIT_BYTES)
            for target in (90, admin):
                assert await _grant(conn, target) == ("unlimited", None, None, None)
    _run(body)


def test_funding_counts_other_finite_grants_once():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, GIB, None, None, budget=2 * GIB + 3 * GIB, reserve=2 * GIB)
        for owner in (77, 78, 79):
            await add_test_account(raw, owner)
        async with pools.control.connection() as conn:
            # 77 and 78 follow the 1 GiB default and 79 has no limit: 2 GiB of 3 GiB funded.
            assert await _set(conn, admin, 79, "unlimited") == "changed"
            # Raising 77 replaces its own grant rather than adding to it.
            assert await _set(conn, admin, 77, "custom", 2 * GIB) == "changed"
            with pytest.raises(errors.RaiseException) as refused:
                await _set(conn, admin, 77, "custom", 2 * GIB + 1)
            assert is_storage_capacity_error(refused.value)
            with pytest.raises(errors.RaiseException) as refused:
                await _set(conn, admin, 79, "custom", MIN_CUSTOM_LIMIT_BYTES)
            assert is_storage_capacity_error(refused.value)
            with pytest.raises(errors.RaiseException):
                await _set(conn, admin, 79, "default")
            assert await _set(conn, admin, 78, "unlimited") == "changed"
            # Exactly budget minus reserve is accepted.
            assert await _set(conn, admin, 77, "custom", 3 * GIB) == "changed"
            with pytest.raises(StorageLimitUnfunded):
                await set_account_storage_limit(
                    conn, {"id": admin, "is_admin": True, "is_enabled": True, "auth_version": 1},
                    78, "default")
            # Lowering is always fundable once it fits.
            assert await _set(conn, admin, 77, "custom", GIB) == "changed"
            assert await _set(conn, admin, 78, "default") == "changed"
            assert await _set(conn, admin, 79, "custom", GIB) == "changed"
            rows = {row[0]: row for row in await _rows(conn, admin)}
        async with raw.connection() as conn:
            assert [await _grant(conn, owner) for owner in (77, 78, 79)] == [
                ("custom", GIB, None, None), ("default", GIB, None, None), ("custom", GIB, None, None)]
            assert await (await conn.execute(
                "SELECT total_grants_bytes FROM public.storage_instance_status()")).fetchone() == (3 * GIB,)
        # Each member could grow to its own grant plus what is left: nothing.
        assert [rows[owner][6] for owner in (77, 78, 79)] == [GIB] * 3
        assert rows[admin][1:5] == (True, "unlimited", None, GIB)
        assert rows[admin][6] == 0
    _run(body)


def test_lowering_below_usage_keeps_data_and_refuses_growth():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        member = await add_test_account(raw, 77)
        async with member.connection() as conn:
            await conn.execute(
                "INSERT INTO raw_messages(account_id,payload) "
                "SELECT 77, jsonb_build_object('x', repeat('a', %s)) FROM generate_series(1,2)",
                (6 * MIB,))
            before = await storage_status(conn)
        assert before["total_bytes"] > MIN_CUSTOM_LIMIT_BYTES
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", MIN_CUSTOM_LIMIT_BYTES) == "changed"
            rows = {row[0]: row for row in await _rows(conn, admin)}
        assert rows[77][2:4] == ("custom", MIN_CUSTOM_LIMIT_BYTES)
        assert rows[77][5] == before["total_bytes"]
        async with member.connection() as conn:
            assert await (await conn.execute("SELECT count(*) FROM raw_messages")).fetchone() == (2,)
            status = await storage_status(conn)
            assert status["total_bytes"] == before["total_bytes"]
            assert status["account_blocked"] and status["raw_blocked"]
            assert status["account_limit_bytes"] == MIN_CUSTOM_LIMIT_BYTES
        with pytest.raises(psycopg.errors.RaiseException) as refused:
            async with member.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(77,'{}')")
        assert is_storage_capacity_error(refused.value)
        async with member.connection() as conn:
            # Shrinking still works over the limit.
            await conn.execute("DELETE FROM raw_messages WHERE id=(SELECT min(id) FROM raw_messages)")
            assert (await storage_status(conn))["total_bytes"] < before["total_bytes"]
    _run(body)


def test_startup_keeps_custom_and_unlimited_edits():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        for owner in (77, 78, 79):
            await add_test_account(raw, owner)
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", 3 * GIB) == "changed"
            assert await _set(conn, admin, 78, "unlimited") == "changed"
        await prepare_application_roles(TEST_DB, storage_config=_limits(GIB, 256 * MIB, 128 * MIB))
        async with raw.connection() as conn:
            assert [await _grant(conn, owner) for owner in (77, 78, 79)] == [
                ("custom", 3 * GIB, 256 * MIB, 128 * MIB), ("unlimited", None, None, None),
                ("default", GIB, 256 * MIB, 128 * MIB)]
        await prepare_application_roles(TEST_DB, storage_config=_limits())
        async with raw.connection() as conn:
            # A custom grant keeps its account limit; its sub-limits follow the policy.
            assert [await _grant(conn, owner) for owner in (77, 78, 79)] == [
                ("custom", 3 * GIB, None, None), ("unlimited", None, None, None),
                ("default", None, None, None)]
            assert await _grant(conn, admin) == ("unlimited", None, None, None)
    _run(body)


def test_restart_applies_policy_sub_limits_to_custom_grants():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        await add_test_account(raw, 77)
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", GIB) == "changed"
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("custom", GIB, 256 * MIB, 128 * MIB)
        await prepare_application_roles(TEST_DB, storage_config=_limits())
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("custom", GIB, None, None)
        await prepare_application_roles(TEST_DB, storage_config=_limits(4 * GIB, 3 * GIB, 512 * MIB))
        async with raw.connection() as conn:
            # The raw default exceeds the custom limit, so it is clamped to it.
            assert await _grant(conn, 77) == ("custom", GIB, GIB, 512 * MIB)
        async with pools.control.connection() as conn:
            # The setter applies the same rule, so an unchanged edit stays unchanged.
            assert await _set(conn, admin, 77, "custom", GIB) == "unchanged"
    _run(body)


@pytest.mark.parametrize("tamper", [
    "raw_limit_bytes=raw_limit_bytes-1", "enhancement_limit_bytes=NULL",
])
def test_startup_refuses_custom_sub_limits_that_do_not_follow_policy(tamper):
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        await add_test_account(raw, 77)
        async with pools.control.connection() as conn:
            assert await _set(conn, admin, 77, "custom", GIB) == "changed"
        await prepare_application_roles(TEST_DB)
        async with raw.connection() as conn:
            await conn.execute(f"UPDATE storage_grants SET {tamper} WHERE account_id=77")
        with pytest.raises(RoleSetupError, match="custom storage grant policy"):
            await prepare_application_roles(TEST_DB)
        # A configured restart reapplies the policy and starts again.
        await prepare_application_roles(TEST_DB, storage_config=_limits(2 * GIB, 256 * MIB, 128 * MIB))
        async with raw.connection() as conn:
            assert await _grant(conn, 77) == ("custom", GIB, 256 * MIB, 128 * MIB)
    _run(body)


def test_reader_reports_grants_usage_and_fundable_room():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB)
        member = await add_test_account(raw, 77)
        await add_test_account(raw, 78)
        actor = {"id": admin, "is_admin": True, "is_enabled": True, "auth_version": 1}
        async with pools.control.connection() as conn:
            assert await set_account_storage_limit(conn, actor, 78, "custom", 3 * GIB) == "changed"
            storage = await list_account_storage(conn, actor)
        async with member.connection() as conn:
            used = (await storage_status(conn))["total_bytes"]
        assert list(storage) == [admin, 77, 78]
        assert storage[admin] == {
            "is_admin": True, "limit_mode": "unlimited", "account_limit_bytes": None,
            "default_limit_bytes": 2 * GIB, "used_bytes": storage[admin]["used_bytes"],
            "fundable_bytes": 10 * GIB - 5 * GIB,
        }
        assert storage[77] == {
            "is_admin": False, "limit_mode": "default", "account_limit_bytes": 2 * GIB,
            "default_limit_bytes": 2 * GIB, "used_bytes": used, "fundable_bytes": 10 * GIB - 3 * GIB,
        }
        assert storage[78]["limit_mode"] == "custom"
        assert storage[78]["fundable_bytes"] == 10 * GIB - 2 * GIB
        async with pools.control.connection() as conn:
            for bad in ({**actor, "auth_version": 2}, {**actor, "is_admin": False}):
                with pytest.raises(StorageLimitUnavailable):
                    await list_account_storage(conn, bad)
                with pytest.raises(StorageLimitUnavailable):
                    await set_account_storage_limit(conn, bad, 77, "unlimited")
            for mode, size in (("custom", None), ("default", GIB), ("custom", MIN_CUSTOM_LIMIT_BYTES - 1),
                               ("custom", True), ("other", None)):
                with pytest.raises(StorageLimitUnavailable):
                    await set_account_storage_limit(conn, actor, 77, mode, size)
    _run(body)


def test_upgrade_from_schema_46_adds_the_editor_without_reprovisioning(monkeypatch, tmp_path):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            await drop_and_recreate_schema(raw)
            historical_dir = tmp_path / "migrations"
            historical_dir.mkdir()
            for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if int(source.name.split("_", 1)[0]) <= 46:
                    shutil.copy(source, historical_dir / source.name)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", historical_dir)
            await run_migrations(raw)
            await prepare_application_roles(TEST_DB)
            async with raw.connection() as conn:
                await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) VALUES "
                                   "(1,'admin@example.invalid','unused',true),"
                                   "(2,'member@example.invalid','unused',false)")
                grants = await (await conn.execute(
                    "SELECT * FROM storage_grants ORDER BY account_id")).fetchall()
                assert await (await conn.execute(
                    "SELECT to_regprocedure(%s),to_regprocedure(%s)", (SETTER, READER))).fetchone() == (None, None)
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await run_migrations(raw)

            async def forbidden_reprovision(conn):
                raise AssertionError("upgrade must validate rather than provision")

            with monkeypatch.context() as patch:
                patch.setattr(application_roles, "_provision", forbidden_reprovision)
                await prepare_application_roles(TEST_DB)
            async with raw.connection() as conn:
                assert await (await conn.execute(
                    "SELECT * FROM storage_grants ORDER BY account_id")).fetchall() == grants
                assert await (await conn.execute(
                    "SELECT has_function_privilege('odograph_control',%s,'EXECUTE'),"
                    "has_function_privilege('odograph_runtime',%s,'EXECUTE'),"
                    "pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=%s::regprocedure))",
                    (SETTER, SETTER, SETTER))).fetchone() == (True, False, "odograph_migrate")
        finally:
            monkeypatch.setattr(db_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
            await restore_test_schema(raw)
            await raw.close()
    asyncio.run(scenario())


def test_setter_runs_concurrently_with_account_creation_configure_and_charged_writes():
    async def body(raw, pools, admin):
        async with raw.connection() as conn:
            await _configure(conn, 2 * GIB, 256 * MIB, 128 * MIB, budget=100 * GIB)
        member = await add_test_account(raw, 77)

        async def set_limit(size):
            async with pools.control.connection() as conn:
                await _set(conn, admin, 77, "custom", size)

        async def add_member(owner):
            async with raw.connection() as conn:
                await conn.execute("INSERT INTO accounts(id,email,password_hash,is_admin) "
                                   "VALUES(%s,%s,'unused',false)",
                                   (owner, f"account-{owner}@example.invalid"))

        async def configure(raw_bytes):
            async with raw.connection() as conn:
                await _configure(conn, 2 * GIB, raw_bytes, 128 * MIB, budget=100 * GIB)

        async def charge():
            async with member.connection() as conn:
                await conn.execute("INSERT INTO raw_messages(account_id,payload) VALUES(77,'{\"x\":1}')")

        for round_ in range(10):
            results = await asyncio.gather(
                set_limit(GIB + round_ * MIB), add_member(100 + round_),
                configure((256 if round_ % 2 else 512) * MIB), charge(),
                return_exceptions=True)
            assert [result for result in results if isinstance(result, BaseException)] == [], round_
        async with raw.connection() as conn:
            assert (await (await conn.execute("SELECT public.storage_usage_consistent()"))
                    .fetchone())[0]
            assert await _grant(conn, 77) == ("custom", GIB + 9 * MIB, 256 * MIB, 128 * MIB)
            assert await (await conn.execute(
                "SELECT count(*) FROM raw_messages WHERE account_id=77")).fetchone() == (10,)
        # Startup's grant checks, including custom and default policy, still pass.
        await prepare_application_roles(TEST_DB)
    _run(body)
