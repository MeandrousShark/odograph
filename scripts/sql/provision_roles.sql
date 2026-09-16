-- Versioned p0-v1 security contract for the fixed disposable fixture.
-- Role identities and managed secrets are owned by app.role_setup.
DO $provision$
DECLARE
 obj record;
 role_name text;
BEGIN
 IF to_regclass('account_context_p0.accounts') IS NULL
    OR to_regclass('odograph_internal.managed_role_state') IS NULL THEN
  RAISE EXCEPTION 'P0 fixture is missing';
 END IF;
 FOR obj IN SELECT nspname FROM pg_namespace
            WHERE nspname IN ('account_context_p0', 'odograph_internal') LOOP
  EXECUTE format('ALTER SCHEMA %I OWNER TO odograph_migrate', obj.nspname);
  EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', obj.nspname);
  FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname <> 'odograph_migrate' LOOP
   EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', obj.nspname, role_name);
   EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', obj.nspname, role_name);
   EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', obj.nspname, role_name);
   EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', obj.nspname, role_name);
  END LOOP;
  EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', obj.nspname);
  EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', obj.nspname);
  EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', obj.nspname);
 END LOOP;
 -- Table-level REVOKE does not remove separate column grants.
 FOR obj IN
  SELECT n.nspname, c.relname, a.attname, x.grantee
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace
  CROSS JOIN LATERAL aclexplode(a.attacl) x
  WHERE n.nspname IN ('account_context_p0','odograph_internal')
    AND x.grantee <> (SELECT oid FROM pg_roles WHERE rolname='odograph_migrate')
 LOOP
  role_name := CASE WHEN obj.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(obj.grantee)) END;
  EXECUTE format('REVOKE ALL (%I) ON %I.%I FROM %s', obj.attname,obj.nspname,obj.relname,role_name);
 END LOOP;
 FOR obj IN SELECT n.nspname,c.relname,c.relkind FROM pg_class c
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname IN ('account_context_p0','odograph_internal') AND c.relkind IN ('r','S')
 ORDER BY c.relkind DESC LOOP
  IF obj.relkind='r' THEN
   EXECUTE format('ALTER TABLE %I.%I OWNER TO odograph_migrate',obj.nspname,obj.relname);
   EXECUTE format('ALTER TABLE %I.%I DISABLE ROW LEVEL SECURITY',obj.nspname,obj.relname);
   EXECUTE format('ALTER TABLE %I.%I NO FORCE ROW LEVEL SECURITY',obj.nspname,obj.relname);
  ELSE
   EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO odograph_migrate',obj.nspname,obj.relname);
  END IF;
 END LOOP;
 FOR obj IN SELECT schemaname,tablename,policyname FROM pg_policies
 WHERE schemaname IN ('account_context_p0','odograph_internal') LOOP
  EXECUTE format('DROP POLICY %I ON %I.%I',obj.policyname,obj.schemaname,obj.tablename);
 END LOOP;
 EXECUTE format('REVOKE CREATE ON DATABASE %I FROM PUBLIC',current_database());
 FOREACH role_name IN ARRAY ARRAY['odograph_control','odograph_runtime'] LOOP
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I',current_database(),role_name);
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I',current_database(),role_name);
 END LOOP;
END
$provision$;
GRANT ALL ON SCHEMA account_context_p0,odograph_internal TO odograph_migrate;
GRANT ALL ON ALL TABLES IN SCHEMA account_context_p0,odograph_internal TO odograph_migrate;
GRANT ALL ON ALL SEQUENCES IN SCHEMA account_context_p0,odograph_internal TO odograph_migrate;
GRANT USAGE ON SCHEMA account_context_p0, odograph_internal TO odograph_control, odograph_runtime;
GRANT SELECT ON odograph_internal.recovery_metadata TO odograph_control, odograph_runtime;
GRANT SELECT ON account_context_p0.accounts, account_context_p0.bootstrap_state TO odograph_control;
GRANT UPDATE(auth_version,is_enabled) ON account_context_p0.accounts TO odograph_control;
GRANT SELECT,INSERT,UPDATE,DELETE ON account_context_p0.vehicles,account_context_p0.ledger TO odograph_runtime;
GRANT USAGE,SELECT ON SEQUENCE account_context_p0.vehicles_id_seq,account_context_p0.ledger_id_seq TO odograph_runtime;
ALTER TABLE account_context_p0.vehicles ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_context_p0.vehicles FORCE ROW LEVEL SECURITY;
ALTER TABLE account_context_p0.ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_context_p0.ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY odograph_account_write ON account_context_p0.vehicles FOR ALL TO odograph_runtime
 USING (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint)
 WITH CHECK (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint);
CREATE POLICY odograph_account_write ON account_context_p0.ledger FOR ALL TO odograph_runtime
 USING (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint)
 WITH CHECK (account_id = NULLIF(current_setting('app.account_id',true),'')::bigint);
CREATE POLICY odograph_bootstrap_owner ON account_context_p0.vehicles FOR INSERT TO odograph_bootstrap WITH CHECK(true);

GRANT USAGE ON SCHEMA account_context_p0,odograph_internal TO odograph_bootstrap;
GRANT SELECT,INSERT ON account_context_p0.accounts TO odograph_bootstrap;
GRANT SELECT,UPDATE ON account_context_p0.bootstrap_state,odograph_internal.provision_authorizations TO odograph_bootstrap;
GRANT INSERT ON account_context_p0.vehicles TO odograph_bootstrap;
GRANT USAGE ON SEQUENCE account_context_p0.accounts_id_seq,account_context_p0.vehicles_id_seq TO odograph_bootstrap;

CREATE OR REPLACE FUNCTION odograph_internal.create_new_account(email text, administrator boolean)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE new_id bigint;
BEGIN
 INSERT INTO account_context_p0.accounts(email,is_admin) VALUES(email,administrator) RETURNING id INTO new_id;
 INSERT INTO account_context_p0.vehicles(account_id,name) VALUES(new_id,'Default vehicle');
 RETURN new_id;
END
$body$;
CREATE OR REPLACE FUNCTION account_context_p0.bootstrap_first_account(email text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE first_id bigint; new_id bigint;
BEGIN
 SELECT first_account_id INTO first_id FROM account_context_p0.bootstrap_state WHERE id=1 FOR UPDATE;
 IF NOT FOUND OR first_id IS NOT NULL OR EXISTS(SELECT 1 FROM account_context_p0.accounts) THEN
  RAISE EXCEPTION 'initial account provisioning is closed';
 END IF;
 new_id := odograph_internal.create_new_account(email,true);
 UPDATE account_context_p0.bootstrap_state SET first_account_id=new_id,completed_at=clock_timestamp() WHERE id=1;
 RETURN new_id;
END
$body$;
CREATE OR REPLACE FUNCTION account_context_p0.provision_authorized_account(grant_id uuid,email text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $body$
DECLARE grant_row odograph_internal.provision_authorizations%ROWTYPE; new_id bigint;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM account_context_p0.bootstrap_state WHERE id=1 AND first_account_id IS NOT NULL) THEN
  RAISE EXCEPTION 'initial account provisioning is incomplete';
 END IF;
 SELECT * INTO grant_row FROM odograph_internal.provision_authorizations
 WHERE id=grant_id FOR UPDATE;
 IF NOT FOUND OR grant_row.account_id IS NOT NULL THEN
  RAISE EXCEPTION 'account provisioning is not authorized';
 END IF;
 new_id := odograph_internal.create_new_account(email,false);
 UPDATE odograph_internal.provision_authorizations SET account_id=new_id WHERE id=grant_row.id;
 RETURN new_id;
END
$body$;
ALTER FUNCTION odograph_internal.create_new_account(text,boolean) OWNER TO odograph_bootstrap;
ALTER FUNCTION account_context_p0.bootstrap_first_account(text) OWNER TO odograph_bootstrap;
ALTER FUNCTION account_context_p0.provision_authorized_account(uuid,text) OWNER TO odograph_bootstrap;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA odograph_internal,account_context_p0 FROM PUBLIC,odograph_control,odograph_runtime;
GRANT EXECUTE ON FUNCTION account_context_p0.bootstrap_first_account(text),account_context_p0.provision_authorized_account(uuid,text) TO odograph_control;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA odograph_internal,account_context_p0 TO odograph_bootstrap;
