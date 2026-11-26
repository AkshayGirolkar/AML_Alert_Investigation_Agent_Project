"""Bearer-token verification (F11). Cognito-compatible: RS256 via JWKS, or HS256 for dev.

`TokenVerifier.verify` returns a `Principal` or raises `AuthError`. Signature, expiry,
issuer and (optionally) audience are checked by PyJWT; Cognito access tokens carry
`client_id` instead of `aud`, so the audience check accepts either.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

ROLES = frozenset({"analyst", "supervisor"})


class AuthError(Exception):
    """The token is missing, malformed, expired or otherwise not acceptable (401)."""


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]
    authenticated: bool = True

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


# Auth mode "off": local development, load tests. Acts as a supervisor, unidentified.
ANONYMOUS = Principal("anonymous", "anonymous", ROLES, authenticated=False)

_USERNAME_CLAIMS = ("username", "cognito:username", "preferred_username", "sub")
KeyResolver = Callable[[str], Any]  # token -> verification key (blocking)


@dataclass(frozen=True)
class VerifierConfig:
    secret: str = ""  # HS256 shared secret (dev); empty = use JWKS
    jwks_url: str = ""  # Cognito: .../{pool_id}/.well-known/jwks.json
    issuer: str = ""
    audience: str = ""  # app client id; empty = not checked
    roles_claim: str = "cognito:groups"
    leeway_seconds: int = 10


class TokenVerifier:
    def __init__(
        self, config: VerifierConfig, key_resolver: KeyResolver | None = None
    ) -> None:
        if not (config.secret or config.jwks_url or key_resolver):
            raise ValueError("auth needs AUTH_JWT_SECRET or AUTH_JWKS_URL")
        self._cfg = config
        if key_resolver is not None:
            self._resolve: KeyResolver = key_resolver
            self._algorithms = ["RS256"]
        elif config.secret:
            self._resolve = lambda _token: config.secret
            self._algorithms = ["HS256"]
        else:
            client = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600)
            self._resolve = lambda token: client.get_signing_key_from_jwt(token).key
            self._algorithms = ["RS256"]

    async def verify(self, token: str) -> Principal:
        try:
            # Key lookup may fetch the JWKS over the network: keep it off the event loop.
            key = await asyncio.to_thread(self._resolve, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,  # never trust the token's own `alg`
                issuer=self._cfg.issuer or None,
                options={
                    "require": ["exp", "sub"],
                    "verify_aud": False,  # handled below (aud or client_id)
                    "verify_iss": bool(self._cfg.issuer),
                },
                leeway=self._cfg.leeway_seconds,
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        if self._cfg.audience and self._cfg.audience not in (
            _as_list(claims.get("aud")) + _as_list(claims.get("client_id"))
        ):
            raise AuthError("invalid token: wrong audience")
        roles = frozenset(_as_list(claims.get(self._cfg.roles_claim))) & ROLES
        username = next(
            (str(claims[c]) for c in _USERNAME_CLAIMS if claims.get(c)), "unknown"
        )
        return Principal(str(claims["sub"]), username, roles)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list | tuple) else [str(value)]
