# F11 – Auth and RBAC

## Goal
Make the API identify callers and enforce roles, so every analyst decision is attributed to
a verified user instead of a name typed into the request body.

## In scope
- Bearer JWT verification (`app/auth/tokens.py`): RS256 through a JWKS URL (Cognito) or an
  HS256 shared secret (local development only). Checks signature, `exp`, `sub`, issuer and,
  if configured, audience (`aud` or Cognito's `client_id`). The algorithm comes from
  settings, never from the token header.
- Roles from the `cognito:groups` claim (configurable): `analyst`, `supervisor`.
- FastAPI dependencies (`app/auth/deps.py`): `current_principal`, `require_role`.
- Route rules: all case/alert routes (start, read, SSE events, decision) need `analyst` or
  `supervisor`; `/health`, `/ready`, `/metrics` stay open.
- Decisions: the recorded analyst is the token's user. A body `analyst_id` must match it
  (403 otherwise). A case whose verdict `risk_score` is at or above
  `AUTH_SUPERVISOR_MIN_RISK` (0.9) can only be decided by a supervisor (four-eyes on
  high-risk cases).
- Audit: the existing `audit_log` decision row now also stores the roles and token subject.
- `AUTH_MODE=off|jwt` (default `off`): off keeps local tools, load tests and the smoke test
  working; the caller must then send `analyst_id`.
- `python -m scripts.mint_token --user ana --role analyst` for dev tokens; scripts that call
  the API send `API_TOKEN` as a bearer token.

## Out of scope
Creating Cognito users (done by an administrator with the AWS CLI); login UI and refresh tokens (the console); per-case ownership; the console relay path, which
authenticates through its own database role (F10.5).

## Follow-up
`infra/cognito.tf` (flag `enable_cognito`, default off) creates the user pool, app client and the `analyst`/`supervisor` groups and switches the ECS tasks to `AUTH_MODE=jwt`. `scripts/aws_cognito_token.sh` logs in with the AWS CLI. Not validated with `terraform validate` or applied (Terraform is not installed here).

## Interfaces
- `TokenVerifier(config).verify(token) -> Principal` or raises `AuthError`.
- `Principal(subject, username, roles, authenticated)`; auth off yields an anonymous
  supervisor with `authenticated=False`.
- Settings: `AUTH_MODE`, `AUTH_JWKS_URL`, `AUTH_JWT_SECRET`, `AUTH_ISSUER`, `AUTH_AUDIENCE`,
  `AUTH_ROLES_CLAIM`, `AUTH_SUPERVISOR_MIN_RISK`.

## Acceptance criteria
- Missing, malformed, expired, wrongly signed, wrong-issuer, wrong-audience and `alg=none`
  tokens give 401 with `WWW-Authenticate: Bearer`; a valid token with no known role gives 403.
- An HS256 token cannot pass an RS256 verifier (algorithm confusion).
- The decision is recorded under the token's user; a mismatching body `analyst_id` is 403.
- Decisions on high-risk cases are 403 for analysts and accepted for supervisors.
- With `AUTH_MODE=off` all F07/F08 behaviour is unchanged.
- ruff, mypy and pytest pass.
