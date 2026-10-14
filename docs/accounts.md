# Account management

This guide covers changing and recovering your password, signing out, and
managing your login email. The last section describes administrator account
management, which is available only in controlled activation fixtures. For
linking an identity provider, see [OIDC sign-in](oidc.md).

## Change your password

While signed in, open **Settings**, then **Account Settings**, and use
**Change password** under **Security**.

## Reset a forgotten password

If your login email is verified, choose **Forgot password?** on the sign-in
page to reset a forgotten password. This needs SMTP and `APP_URL`; see
[Email](configuration.md#email). The link goes only to your verified login
email and expires in 30 minutes. Completing a reset signs that account out of
every Odograph session, including the browser that used the link, and returns
you to sign-in.

In a controlled multi-account fixture, an enabled administrator can request a
reset for another account in **Admin > Accounts**. The link goes only to that
account's stored, verified login email; the administrator never sees it or
chooses a password. Without a verified address, use the trusted host-local
recovery command in [Recover from the server](#recover-from-the-server).

## Recover from the server

Otherwise, reset it from the server. Find the account ID, then confirm the
account shown and type the new password when prompted:

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

A password change signs out your other Odograph sessions; the browser that made
the change stays signed in. An operator reset signs out every session for that
account. Neither changes a linked sign-in provider, verified email or tracking
devices.

**Sign out everywhere** in Account Settings ends every Odograph browser
session for your account, including the one you are using. It does not end a
sign-in provider session or revoke tracking-device credentials.

Establishing a password for an OIDC-only account and managing its sign-in
methods are currently limited to controlled activation fixtures. They will be
available for normal installations only after supported account activation is
released.

## Login email

In **Settings > Account Settings**, you can verify your current login email or
change it after confirming the new address. These actions require your current
password and configured email delivery. Until a new address is confirmed, the
current email remains the login. A confirmed change signs out other Odograph
sessions; it does not change the linked OIDC identity or saved notification
destinations. Email verification is not a password reset flow.

Changing your login email is separate from password recovery. The new address
becomes the login only after you confirm the emailed challenge.

## Administrator account management (controlled activation only)

The flows in this section are implemented for controlled activation fixtures.
Normal installations stay single-account and cannot use them until
multi-account activation is separately released.

### Member invitations

In controlled activation fixtures, an enabled administrator can issue, resend,
and revoke member invitations in **Admin > Accounts**. The new invitation link
and manual token are shown once; optional email sends that same invitation.
Resending creates a fresh token and invalidates the old one. The normal
single-account installation cannot use invitations to add accounts until
multi-account activation is separately released.

### Disable and re-enable an account

In those fixtures, an administrator can also disable or re-enable another
account. Disabling ends its app sessions and revokes its pending security
proofs, invitations it issued, and tracking credentials. Re-enabling allows
a fresh sign-in with a retained login method, but does not restore old sessions,
links or tracking credentials. At least one usable administrator must remain.

### Scheduled deletion and purge

An administrator can also schedule another account for deletion in those
fixtures by typing its login email and acknowledging the export and backup
effects. Scheduling immediately disables the account and starts a 30-day
grace period. An enabled administrator can cancel during that period; there is
no automatic purge. After the grace period, permanent purge requires fresh
administrator authentication with a password or the exact linked OIDC
identity, the target login email, and a separate confirmation. Administrators
cannot delete themselves, and a usable administrator must remain. The account
owner can download a portable export before disablement or after cancellation.
It includes the ledger, rates and basic preferences, but omits notification
destinations, schedules and raw points. Purge removes data from the live
database only; retained backups may still contain it. Restoring an older
backup can reintroduce the account and its data. These flows remain unavailable
in normal installations until multi-account activation is separately released.
