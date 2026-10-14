# OIDC sign-in

OIDC can be linked as a second way to sign in to an existing account. The
administrator configures one OIDC provider for the instance. OIDC is optional;
Odograph works without it.

## Set up the provider

Register `https://<your-domain>/auth/callback` with your provider, for example
`https://mileage.example.com/auth/callback`, and set the three OIDC variables
`OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. See the
[authentication settings](configuration.md#authentication-and-ingest) for
details.

## Link it to your account

To link it to an existing password account, open **Settings > Account
Settings** while signed in, enter your current password, and authorize with the
provider.

After linking, either method signs you into the same account. The link follows
the provider's issuer and subject, so it survives your email address changing
there.

## Fresh authentication

When enabled, OIDC-only security actions require fresh provider authentication
with a valid `auth_time` from a provider that honors `max_age=0`. If the
provider does not meet this, those actions fail closed.

## Controlled activation only

Invitation redemption without a local password and OIDC-only method
management are implemented for controlled activation fixtures. Normal
installations retain the single-account database guard; these flows are not
available for regular use until a separately reviewed activation migration is
released. Establishing a password for an OIDC-only account is limited the same
way; see [Account management](accounts.md).

## Signing out

If you use OIDC, signing out of Odograph does not sign you out of your identity
provider, so signing back in may not prompt you at all. Sign out of the
provider separately to end that session. Odograph does not keep provider tokens
after sign-in.

## Older OIDC-only installations

If you are moving an older OIDC-only installation, read
[Upgrading](upgrading.md) first.
