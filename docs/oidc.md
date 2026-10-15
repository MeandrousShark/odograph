# OIDC sign-in

OIDC can be linked as a second way to sign in to an existing account. The
operator configures one OIDC provider for the instance in the environment
(`.env`); there is no provider setting in the web interface. OIDC is optional;
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

## Invited members

An invited member can accept the invitation with the configured provider
instead of setting a local password. That creates an OIDC-only account, which
can add a password later; see [Account management](accounts.md).

Accepting an invitation this way makes the invited address the login email,
unverified. The provider's email is not matched against it. Once any account
exists, an OIDC sign-in that is not linked to an account or an invitation is
refused. Failures show the sign-in or invitation page with the message.

## Signing out

If you use OIDC, signing out of Odograph does not sign you out of your identity
provider, so signing back in may not prompt you at all. Sign out of the
provider separately to end that session. Odograph does not keep provider tokens
after sign-in.

## Older OIDC-only installations

If you are moving an older OIDC-only installation, read
[Upgrading](upgrading.md) first.
