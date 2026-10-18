# Account management

This guide covers changing and recovering your password, signing out, and
managing your login email. The last section describes administrator account
management: inviting members, disabling accounts and deleting them. For
linking an identity provider, see [OIDC sign-in](oidc.md).

## Change your password

While signed in, open **Settings**, then **Account Settings**, and use
**Change password** under **Security**.

## Reset a forgotten password

If your login email is verified, choose **Forgot password?** on the sign-in
page to reset a forgotten password. This needs `SMTP_HOST`, `EMAIL_FROM` and
`APP_URL`; see
[Email](configuration.md#email). The link goes only to your verified login
email and expires in 30 minutes. Completing a reset signs that account out of
every Odograph session, including the browser that used the link, and returns
you to sign-in.

An enabled administrator can request a
reset for another account from **Accounts** in the header. The link goes only to that
account's stored, verified login email; the administrator never sees it or
chooses a password. Without a verified address, use the trusted host-local
recovery command in [Recover from the server](#recover-from-the-server).

## Recover from the server

Otherwise, reset it from the server. Find the account ID, then confirm the
account shown and type the new password when prompted. The command refuses a
disabled account:

```sh
docker compose exec app python -m app.manage_account list-accounts
docker compose exec app python -m app.manage_account reset-password ACCOUNT_ID
# or: podman-compose exec app python -m app.manage_account list-accounts
# or: podman-compose exec app python -m app.manage_account reset-password ACCOUNT_ID
```

Use `create-admin` only before the first account has been created, when public
signup is closed. Completing bootstrap permanently closes first-account
creation, even if the original administrator is later purged. Restoring an
earlier database backup can restore a purged account and its data; there is no
post-bootstrap `create-admin` recovery path.

## Sessions and sign out everywhere

Sessions are signed cookies that last 14 days. Logging out clears only this
browser's cookie; a copied cookie stays valid until you use **Sign out
everywhere**, your password changes, another change increments the account's
authentication version (OIDC link or unlink, confirmed login-email change), or
the operator rotates `SESSION_SECRET`.
A password change signs out your other Odograph sessions; the browser that made
the change stays signed in. An operator reset signs out every session for that
account. Neither changes a linked sign-in provider, verified email or tracking
devices.

**Sign out everywhere** in Account Settings ends every Odograph browser
session for your account, including the one you are using. It does not end a
sign-in provider session or revoke tracking-device credentials.

An account created by accepting an invitation with the identity provider has
no password at first. It can add one in Account Settings after
reauthenticating with that provider.

## Login email

In **Settings > Account Settings**, you can verify your current login email or
change it after confirming the new address. These actions require your current
password, or provider reauthentication if the account has no password, and
configured email delivery. Until a new address is confirmed, the
current email remains the login. A confirmed change signs out other Odograph
sessions; it does not change the linked OIDC identity or saved notification
destinations. Email verification is not a password reset flow.

Changing your login email is separate from password recovery. The new address
becomes the login only after you confirm the emailed challenge.

## Administrator account management

An installation has one administrator, the first account. Everyone else joins
as a member by invitation; there is no open signup. Each account's trips,
places, vehicles and settings are private to it, including from the
administrator.

### Member invitations

An enabled administrator can issue, resend, and revoke member invitations in
**Accounts** in the header. Invitations expire after 48 hours. The new
invitation is shown once: a full link when `APP_URL` is set, otherwise the
one-time token to paste on the invitation page. Optional email sends that same
invitation. Resending creates a fresh token and invalidates the old one.
Issuing and resending are rate limited: per address, 1 per minute and 5 per 24
hours; per administrator, 10 per 15 minutes and 50 per 24 hours.

### Account limit

Storage limits are off by default, so the number of member accounts is not
limited by storage. The administrator account never has a storage limit.

When `STORAGE_ACCOUNT_LIMIT_BYTES` is set, each member account receives that
allowance, funded from the instance budget. With a 2 GiB account limit, the
default budget funds five members. When no allowance is left, creating an
invitation is refused with a message naming `STORAGE_INSTANCE_BUDGET_BYTES`,
and an invitation accepted after the budget ran out is refused without being
used up. To admit more accounts, lower or remove member limits in
**Accounts**, which takes effect immediately, or purge an account that is no
longer needed. Otherwise raise `STORAGE_INSTANCE_BUDGET_BYTES` or lower
`STORAGE_ACCOUNT_LIMIT_BYTES` and restart. A member who reaches their
allowance is told to ask an administrator. See
[Logical storage accounting](configuration.md#logical-storage-accounting).

### Edit a member's storage limit

The **Storage** column in **Accounts** shows each account's usage and limit.
An enabled administrator can change a member's limit from **Change storage
limit** below that member's row. The change takes effect immediately, without
a restart:

- **Instance default** follows `STORAGE_ACCOUNT_LIMIT_BYTES`, or no limit when
  it is unset. A restart that changes the default updates these accounts.
- **No limit** removes the account's limits and uses none of the instance
  budget.
- **Custom** sets the account limit in GiB, from 0.01 GiB with up to two
  decimal places. The raw and enhancement limits follow their configured
  defaults, capped at the custom limit. A restart keeps the custom account
  limit and reapplies the configured raw and enhancement defaults to it.

A finite limit must fit the instance budget: budget minus reserve, less every
other account's finite limit. The form shows the largest size that fits.
Administrator accounts always have no limit and cannot be edited.

Lowering a limit below current usage keeps all existing data. The member sees
the account as over its allowance, new data that adds storage is refused, and
deleting data still works.

### Disable and re-enable an account

An administrator can also disable or re-enable another
account. Disabling ends its app sessions and revokes its pending security
proofs, invitations it issued, and tracking credentials. Re-enabling allows
a fresh sign-in with a retained login method, but does not restore old sessions,
links or tracking credentials. At least one usable administrator must remain.
Re-enabling is blocked while a deletion deadline is set.

### Scheduled deletion and purge

An administrator can also schedule another account for deletion by typing its
login email and acknowledging the export and backup
effects. Scheduling immediately disables the account and starts a 30-day
grace period. An enabled administrator can cancel during that period; there is
no automatic purge. After the grace period the account can only be purged: it
can no longer be cancelled or re-enabled. Permanent purge requires fresh
administrator authentication with a password or the exact linked OIDC
identity, the target login email, and a separate confirmation. Administrators
cannot delete themselves, and a usable administrator must remain. The account
owner can download a portable export before disablement or after cancellation.
It includes the ledger, rates and basic preferences, but omits notification
destinations, schedules and raw points. Purge removes the account's data from
the live database, except that the invitation the member accepted stays in the
invitation list (invitee email, status and times) and security audit rows
(IDs, action, outcome and time) remain until the 365-day audit retention
prunes them. Retained backups may still
contain purged data, and restoring an older backup can reintroduce the account
and its data.
