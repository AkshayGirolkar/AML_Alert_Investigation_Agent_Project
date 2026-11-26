"""Mint a dev bearer token (F11): python -m scripts.mint_token --user ana --role analyst

Signs with AUTH_JWT_SECRET (HS256), the same secret the API uses when `AUTH_MODE=jwt`
and no JWKS URL is set. For local development and tests only; real deployments use
Cognito. Prints the token so it can be used as `export API_TOKEN=$(...)`.
"""

import argparse
import time

import jwt

from app.config import get_settings


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", required=True)
    p.add_argument("--role", action="append", choices=["analyst", "supervisor"])
    p.add_argument("--ttl", type=int, default=3600, help="seconds")
    a = p.parse_args()
    s = get_settings()
    if not s.auth_jwt_secret:
        raise SystemExit("set AUTH_JWT_SECRET in .env first")
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": f"dev-{a.user}",
        "username": a.user,
        s.auth_roles_claim: a.role or ["analyst"],
        "iat": now,
        "exp": now + a.ttl,
    }
    if s.auth_issuer:
        claims["iss"] = s.auth_issuer
    if s.auth_audience:
        claims["client_id"] = s.auth_audience
    print(jwt.encode(claims, s.auth_jwt_secret, algorithm="HS256"))


if __name__ == "__main__":
    main()
