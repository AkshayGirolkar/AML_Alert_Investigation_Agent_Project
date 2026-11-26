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
- `infra/cognito.tf` creates the pool, an app client and the two groups behind `enable_cognito` (default false, so the dev stack is unchanged). Users are created with the AWS CLI, not Terraform, so passwords stay out of state. With it on, the smoke test and in-VPC load test need `API_TOKEN`.
- Token revocation is not checked; tokens are short-lived.
