-- migration-class: rewrite
-- Supported multi-account operation. Migration 020 limited the installation
-- to one administrator account with a unique index and an is_admin check.
-- Invited members are non-administrator accounts, so both guards go and new
-- rows default to non-administrator. bootstrap_first_account still refuses
-- once any account exists, so only the first account can be an administrator.
-- IF EXISTS keeps the upgrade idempotent for databases that lack either guard.
DROP INDEX IF EXISTS public.accounts_singleton_idx;
ALTER TABLE public.accounts DROP CONSTRAINT IF EXISTS accounts_is_admin_check;
ALTER TABLE public.accounts ALTER COLUMN is_admin SET DEFAULT false;
