"""Unwired role/credential setup and recovery proof for the disposable P0 fixture.

This versioned contract never touches live application tables. DATABASE_URL
is the only installation secret input; managed role passwords are stored in
protected database state and consequently travel with a full database dump.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.account_context import CONTROL_ROLE, MIGRATE_ROLE, RUNTIME_ROLE
from app.db import ROLE_SETUP_ADVISORY_LOCK_KEY

FIXTURE_SCHEMA = "account_context_p0"
SECURITY_CONTRACT_VERSION = "p0-v1"
BOOTSTRAP_ROLE = "odograph_bootstrap"
ALL_ROLES = (MIGRATE_ROLE, BOOTSTRAP_ROLE, CONTROL_ROLE, RUNTIME_ROLE)
SQL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "sql"
SETUP_LOCK = ROLE_SETUP_ADVISORY_LOCK_KEY
SCHEMAS = (FIXTURE_SCHEMA, "odograph_internal")
TABLES = (
    "account_context_p0.accounts", "account_context_p0.bootstrap_state",
    "account_context_p0.vehicles", "account_context_p0.ledger",
    "odograph_internal.managed_role_state", "odograph_internal.recovery_metadata",
    "odograph_internal.provision_authorizations",
)
SEQUENCES = (
    "account_context_p0.accounts_id_seq", "account_context_p0.vehicles_id_seq",
    "account_context_p0.ledger_id_seq",
)
FUNCTIONS = (
    "odograph_internal.create_new_account(text,boolean)",
    "account_context_p0.bootstrap_first_account(text)",
    "account_context_p0.provision_authorized_account(uuid,text)",
)


class RoleSetupError(RuntimeError):
    """A sanitized setup, connection, or security-contract failure."""


@dataclass(frozen=True)
class ManagedRoleState:
    database_name: str
    installation_id: UUID
    contract_version: str = SECURITY_CONTRACT_VERSION
    owner_role: str = MIGRATE_ROLE
    runtime_password: str = field(default="", repr=False)
    control_password: str = field(default="", repr=False)


@dataclass(frozen=True)
class RolePools:
    control: AsyncConnectionPool
    runtime: AsyncConnectionPool


class _SafeConnection(AsyncConnection):
    @classmethod
    async def connect(cls, *args, **kwargs):
        try:
            return await super().connect(*args, **kwargs)
        except Exception:
            raise RoleSetupError("database connection failed") from None


def role_conninfo(database_url: str, state: ManagedRoleState, role: str) -> str:
    """Build a restricted connection internally; never print the result."""
    if role not in (CONTROL_ROLE, RUNTIME_ROLE):
        raise RoleSetupError("unknown restricted identity")
    try:
        params = conninfo_to_dict(database_url)
        params.update(user=role, password=(state.control_password if role == CONTROL_ROLE
                                          else state.runtime_password),
                      dbname=state.database_name, connect_timeout="5")
        return make_conninfo(**params)
    except Exception:
        raise RoleSetupError("invalid database connection configuration") from None


async def _identities(conn, *, quarantine=False):
    created = set()
    for role in ALL_ROLES:
        cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        if await cur.fetchone() is None:
            created.add(role)
            await conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        await conn.execute(sql.SQL(
            "ALTER ROLE {} {} NOSUPERUSER NOBYPASSRLS NOCREATEDB "
            "NOCREATEROLE NOREPLICATION INHERIT"
        ).format(sql.Identifier(role),sql.SQL("LOGIN" if not quarantine and role in (CONTROL_ROLE,RUNTIME_ROLE) else "NOLOGIN")))
        cur = await conn.execute(
            "SELECT parent.rolname FROM pg_auth_members m "
            "JOIN pg_roles parent ON parent.oid=m.roleid "
            "JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=%s", (role,)
        )
        for (parent,) in await cur.fetchall():
            await conn.execute(sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(parent), sql.Identifier(role)))
        cur = await conn.execute(
            "SELECT member.rolname FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member "
            "JOIN pg_roles parent ON parent.oid=m.roleid WHERE parent.rolname=%s", (role,))
        for (member,) in await cur.fetchall():
            await conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(role),sql.Identifier(member)))
        await conn.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(role)))
        cur = await conn.execute(
            "SELECT d.datname FROM pg_db_role_setting s JOIN pg_database d ON d.oid=s.setdatabase "
            "WHERE s.setrole=(SELECT oid FROM pg_roles WHERE rolname=%s)", (role,)
        )
        for (database,) in await cur.fetchall():
            await conn.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(
                sql.Identifier(role), sql.Identifier(database)))

    return created


async def create_restore_role_identities(database_url: str, *, owner_role=MIGRATE_ROLE):
    """Recreate named NOLOGIN identities before restoring role-bearing objects."""
    if owner_role != MIGRATE_ROLE:
        raise RoleSetupError("unsupported fixture owner")
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SETUP_LOCK,))
            await _identities(conn,quarantine=True)
    except Exception:
        raise RoleSetupError("fixture restore role preparation failed") from None


async def _load_state(conn):
    cur = await conn.execute(
        "SELECT current_database(), installation_id, contract_version, owner_role, "
        "runtime_password,control_password FROM odograph_internal.managed_role_state WHERE id=1"
    )
    row = await cur.fetchone()
    if row is None or row[2:4] != (SECURITY_CONTRACT_VERSION, MIGRATE_ROLE):
        raise RoleSetupError("missing or unsupported managed credential state")
    if not all(isinstance(secret, str) and len(secret) >= 32 for secret in row[4:]):
        raise RoleSetupError("invalid managed credential state")
    return ManagedRoleState(*row)


async def _prepare(conn, *, restoring=False):
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SETUP_LOCK,))
    cur = await conn.execute("SELECT count(*) FROM odograph_internal.managed_role_state")
    count = (await cur.fetchone())[0]
    if count == 0:
        if restoring:
            raise RoleSetupError("restored managed credentials are missing")
        await conn.execute(
            "INSERT INTO odograph_internal.managed_role_state VALUES (1,%s,%s,%s,%s,%s)",
            (SECURITY_CONTRACT_VERSION, MIGRATE_ROLE, uuid4(), secrets.token_urlsafe(32), secrets.token_urlsafe(32)),
        )
    state = await _load_state(conn)
    created = await _identities(conn)
    for role, password in ((CONTROL_ROLE,state.control_password),(RUNTIME_ROLE,state.runtime_password)):
        if restoring or count == 0 or role in created:
            verifier = conn.pgconn.encrypt_password(password.encode(),role.encode(),b'scram-sha-256').decode()
            await conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(verifier)))
    await conn.execute("DELETE FROM odograph_internal.recovery_metadata")
    await conn.execute("INSERT INTO odograph_internal.recovery_metadata VALUES(1,%s,%s,%s)",
                       (state.contract_version,state.owner_role,state.installation_id))
    await conn.execute((SQL_DIR / "provision_roles.sql").read_text())
    await _validate_contract(conn, state)
    return state


async def create_p0_fixture(database_url: str) -> ManagedRoleState:
    """Create the fixed disposable schema and managed roles atomically."""
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SETUP_LOCK,))
            await conn.execute((SQL_DIR / "p0_fixture.sql").read_text())
            return await _prepare(conn)
    except Exception:
        raise RoleSetupError("P0 fixture creation failed") from None


async def prepare_fixture_roles(database_url: str) -> ManagedRoleState:
    """Idempotently repair the fixed fixture contract and reuse stored secrets."""
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            return await _prepare(conn)
    except Exception:
        raise RoleSetupError("P0 fixture role setup failed") from None


def _policy_expression(value):
    return re.sub(r"[\s()]", "", value or "")


async def _validate_contract(conn, state):
    """Check effective rights, including PUBLIC, memberships and column ACLs."""
    def require(condition):
        if not condition:
            raise RoleSetupError("P0 database security contract mismatch")

    cur = await conn.execute(
        "SELECT n.nspname||'.'||c.relname, c.relkind,pg_get_userbyid(c.relowner),"
        "c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','S','v','m','f','p')", (list(SCHEMAS),))
    objects = await cur.fetchall()
    require({row[0] for row in objects} == set(TABLES + SEQUENCES))
    for name, kind, owner, enabled, forced in objects:
        require(owner == MIGRATE_ROLE)
        require((enabled,forced) == ((True,True) if name in TABLES[2:4] else (False,False)))
    cur = await conn.execute("SELECT contract_version,owner_role,installation_id FROM odograph_internal.recovery_metadata WHERE id=1")
    require(await cur.fetchone() == (state.contract_version,state.owner_role,state.installation_id))
    cur = await conn.execute(
        "SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolcanlogin "
        "FROM pg_roles WHERE rolname=ANY(%s)", (list(ALL_ROLES),))
    roles = await cur.fetchall()
    require(len(roles) == 4)
    for name, *flags in roles:
        require(flags == [False]*5 + [name in (CONTROL_ROLE,RUNTIME_ROLE)])
    cur = await conn.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member JOIN pg_roles p ON p.oid=m.roleid WHERE r.rolname=ANY(%s) OR p.rolname=ANY(%s)", (list(ALL_ROLES),list(ALL_ROLES)))
    require((await cur.fetchone())[0] == 0)
    cur = await conn.execute(
        "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid=s.setrole WHERE r.rolname=ANY(%s)",
        (list(ALL_ROLES),))
    require((await cur.fetchone())[0] == 0)
    cur = await conn.execute(
        "WITH objects AS ("
        " SELECT c.relowner owner,c.relacl acl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)"
        " UNION ALL SELECT c.relowner,a.attacl FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)"
        " UNION ALL SELECT p.proowner,p.proacl FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s)"
        " UNION ALL SELECT nspowner,nspacl FROM pg_namespace WHERE nspname=ANY(%s))"
        " SELECT count(*) FROM objects o CROSS JOIN LATERAL aclexplode(o.acl) x"
        " WHERE x.grantee=0 OR x.grantee NOT IN (SELECT oid FROM pg_roles WHERE rolname=ANY(%s))"
        " OR (x.is_grantable AND x.grantee<>o.owner)",
        (list(SCHEMAS),list(SCHEMAS),list(SCHEMAS),list(SCHEMAS),list(ALL_ROLES)))
    require((await cur.fetchone())[0] == 0)
    cur = await conn.execute(
        "SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname=ANY(%s)", (list(SCHEMAS),))
    policies = await cur.fetchall()
    require(len(policies)==3)
    expr = _policy_expression("account_id = NULLIF(current_setting('app.account_id'::text,true),''::text)::bigint")
    for schema, table, name, permissive, roles, command, using, check in policies:
        require(schema == FIXTURE_SCHEMA and permissive == 'PERMISSIVE')
        if name == 'odograph_bootstrap_owner':
            require((table,roles,command,using,check)==('vehicles',[BOOTSTRAP_ROLE],'INSERT',None,'true'))
        else:
            require(name=='odograph_account_write' and table in ('vehicles','ledger'))
            require(roles==[RUNTIME_ROLE] and command=='ALL')
            require(_policy_expression(using)==expr and _policy_expression(check)==expr)
    source = (SQL_DIR / "provision_roles.sql").read_text()
    expected_bodies = re.findall(r"AS \$body\$(.*?)\$body\$;", source, re.S)
    cur = await conn.execute(
        "SELECT n.nspname||'.'||p.proname,pg_get_userbyid(p.proowner),p.prosecdef,p.proconfig,p.prosrc "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s)", (list(SCHEMAS),))
    functions = await cur.fetchall()
    require(len(functions)==3)
    expected_names = [f.split('(')[0] for f in FUNCTIONS]
    for name,owner,definer,config,body in functions:
        require(name in expected_names and owner==BOOTSTRAP_ROLE and definer)
        require(config==['search_path=pg_catalog, pg_temp'])
        require(body==expected_bodies[expected_names.index(name)])
    privileges=('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')
    for role in (CONTROL_ROLE,RUNTIME_ROLE,BOOTSTRAP_ROLE):
        cur = await conn.execute("SELECT has_database_privilege(%s,current_database(),'CONNECT'),has_database_privilege(%s,current_database(),'CREATE')",(role,role))
        require(await cur.fetchone()==(True,False))
        for schema in SCHEMAS:
            cur=await conn.execute("SELECT has_schema_privilege(%s,%s,'USAGE'),has_schema_privilege(%s,%s,'CREATE')",(role,schema,role,schema))
            require(await cur.fetchone()==(True,False))
        for table in TABLES:
            allowed = set()
            if table.endswith('recovery_metadata') and role in (CONTROL_ROLE,RUNTIME_ROLE): allowed={'SELECT'}
            if role==CONTROL_ROLE and table in TABLES[:2]: allowed={'SELECT'}
            if role==RUNTIME_ROLE and table in TABLES[2:4]: allowed={'SELECT','INSERT','UPDATE','DELETE'}
            if role==BOOTSTRAP_ROLE:
                if table==TABLES[0]: allowed={'SELECT','INSERT'}
                if table in (TABLES[1],TABLES[6]): allowed={'SELECT','UPDATE'}
                if table==TABLES[2]: allowed={'INSERT'}
            for privilege in privileges:
                cur=await conn.execute("SELECT has_table_privilege(%s,%s,%s)",(role,table,privilege))
                require((await cur.fetchone())[0] == (privilege in allowed))
            cur=await conn.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped",(table,))
            for (column,) in await cur.fetchall():
                for privilege in ('SELECT','INSERT','UPDATE','REFERENCES'):
                    expected=privilege in allowed or (role==CONTROL_ROLE and table==TABLES[0] and column in ('auth_version','is_enabled') and privilege=='UPDATE')
                    cur=await conn.execute("SELECT has_column_privilege(%s,%s,%s,%s)",(role,table,column,privilege))
                    require((await cur.fetchone())[0]==expected)
        for sequence in SEQUENCES:
            allowed=set()
            if role==RUNTIME_ROLE and sequence in SEQUENCES[1:]: allowed={'USAGE','SELECT'}
            if role==BOOTSTRAP_ROLE and sequence in SEQUENCES[:2]: allowed={'USAGE'}
            for privilege in ('USAGE','SELECT','UPDATE'):
                cur=await conn.execute("SELECT has_sequence_privilege(%s,%s,%s)",(role,sequence,privilege))
                require((await cur.fetchone())[0]==(privilege in allowed))
        for index,function in enumerate(FUNCTIONS):
            cur=await conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')",(role,function))
            require((await cur.fetchone())[0]==(role==BOOTSTRAP_ROLE or (role==CONTROL_ROLE and index>0)))


async def validate_role_connection(conn, state: ManagedRoleState, role: str):
    """Validate the authenticated role, endpoint and complete fixture contract."""
    try:
        if role not in (CONTROL_ROLE, RUNTIME_ROLE):
            raise RoleSetupError("unknown restricted identity")
        async with conn.transaction():
            cur=await conn.execute("SELECT session_user,current_user,current_database(),NULLIF(current_setting('app.account_id',true),'')")
            if await cur.fetchone() != (role,role,state.database_name,None):
                raise RoleSetupError("restricted database identity mismatch")
            await _validate_contract(conn,state)
    except Exception:
        raise RoleSetupError("restricted database validation failed") from None


@asynccontextmanager
async def managed_role_pools(database_url: str):
    """Close privileged setup, then expose only validated restricted pools."""
    state=await prepare_fixture_roles(database_url)
    pools=[]
    try:
        for role in (CONTROL_ROLE,RUNTIME_ROLE):
            conninfo=role_conninfo(database_url,state,role)
            async def validate(conn, expected_role=role):
                await validate_role_connection(conn,state,expected_role)
            # Surface bad credentials immediately, without background retry noise.
            async with await _SafeConnection.connect(conninfo) as conn:
                await validate(conn)
            pool=AsyncConnectionPool(conninfo,connection_class=_SafeConnection,
                min_size=1,max_size=6,open=False,configure=validate,check=validate,
                timeout=5,name=f'p0-{role}')
            pools.append(pool)
            await pool.open(wait=True,timeout=5)
    except BaseException as error:
        for pool in reversed(pools):
            await pool.close()
        if not isinstance(error, Exception):
            raise
        raise RoleSetupError("managed database pools failed") from None
    try:
        yield RolePools(control=pools[0],runtime=pools[1])
    finally:
        for pool in reversed(pools):
            await pool.close()


async def finalize_fixture_restore(database_url: str) -> ManagedRoleState:
    """Restore stored credentials and exact grants, then verify real sessions."""
    try:
        async with await _SafeConnection.connect(database_url) as conn:
            state=await _prepare(conn,restoring=True)
        async with managed_role_pools(database_url):
            pass
        return state
    except Exception:
        raise RoleSetupError("P0 fixture recovery incomplete") from None


def main():
    import asyncio
    parser=argparse.ArgumentParser(description="Prepare the fixed disposable P0 fixture only")
    parser.add_argument('--fixture',action='store_true',required=True)
    parser.parse_args()
    url=os.environ.get('DATABASE_URL') or os.environ.get('PROVISION_DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required')
    try:
        asyncio.run(prepare_fixture_roles(url))
    except RoleSetupError as error:
        parser.exit(1,f'{error}\n')
    print('P0 fixture role setup complete.')


if __name__=='__main__':
    main()
