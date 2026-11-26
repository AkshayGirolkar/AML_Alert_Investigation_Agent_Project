# ADR 0011 – F11 Auth and RBAC

## Status
Accepted

## Decisions
- **Verify tokens, don't issue them.** Cognito (or any OIDC provider) issues JWTs; the API only verifies them with PyJWT. RS256 via JWKS is the real path; HS256 with a shared secret exists so development and tests need no identity provider, and is selected only when no JWKS URL is set.
- **Algorithms are pinned by configuration**, not read from the token, which blocks `alg=none` and RS256/HS256 confusion.
- **Audience accepts `aud` or `client_id`**, because Cognito access tokens carry only `client_id`.
- **Identity comes from the token.** The decision's `analyst_id` is the token's username; a differing body value is rejected rather than silently ignored, so a client bug is visible.
- **Supervisor means four-eyes on high risk**, not "can do more endpoints": the design says analysts approve escalations and high-risk closures, so cases at or above a risk threshold need a supervisor. The threshold is a setting.
- **`AUTH_MODE=off` by default.** Load tests, the AWS smoke test and local demos call the API without a token. The cost is that a deployment must set `AUTH_MODE=jwt`; the runbook says so, and startup fails if jwt mode has no key source.
- **Health, readiness and metrics stay open** for the ALB and scrapers.
- **The console relay is unchanged**: it acts through its own database role (ADR 0005), not HTTP.

## Consequences
- `EventSource` in browsers cannot set an Authorization header, so the console's SSE client must use fetch-based streaming.
- Terraform does not yet create a Cognito pool; until it does, real AWS runs use `AUTH_MODE=off` behind the IP-restricted ALB, or a pool created by hand with `AUTH_JWKS_URL`/`AUTH_ISSUER` set.
- Token revocation is not checked; tokens are short-lived.
