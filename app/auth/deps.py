"""FastAPI dependencies for authentication and role checks (F11).

`app.state.verifier` is None when `AUTH_MODE=off`; every dependency then yields the
anonymous supervisor principal, so local tools keep working without tokens.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.auth.tokens import ANONYMOUS, AuthError, Principal, TokenVerifier
from app.observability.metrics import metrics

log = logging.getLogger("auth")


def _deny(status: int, reason: str, detail: str) -> HTTPException:
    metrics.inc("auth_denied_total", reason=reason)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(status, detail, headers=headers)


async def current_principal(request: Request) -> Principal:
    verifier: TokenVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        return ANONYMOUS
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _deny(401, "missing_token", "missing bearer token")
    try:
        return await verifier.verify(token.strip())
    except AuthError as exc:
        log.warning("auth rejected: %s", exc)
        raise _deny(401, "invalid_token", "invalid or expired token") from exc


def require_role(*roles: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency: the caller must hold at least one of `roles`."""

    async def dep(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.has_role(*roles):
            log.warning("auth forbidden: %s lacks %s", principal.username, roles)
            raise _deny(403, "forbidden", f"requires role: {' or '.join(roles)}")
        return principal

    return dep


ANY_ROLE = require_role("analyst", "supervisor")
