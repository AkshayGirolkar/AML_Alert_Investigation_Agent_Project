"""F11 unit tests: token verification, role checks, and the decision rules (no DB)."""

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.tokens import AuthError, TokenVerifier, VerifierConfig
from app.main import create_app
from tests.unit.test_api import FakeQueue, FakeStore

SECRET = "x" * 40
ISS = "https://issuer.test/pool"
CFG = VerifierConfig(secret=SECRET, issuer=ISS, audience="app-client")


def token(
    *,
    user: str = "ana",
    groups: list[str] | None = None,
    exp_in: int = 600,
    secret: str = SECRET,
    alg: str = "HS256",
    **extra: Any,
) -> str:
    claims: dict[str, Any] = {
        "sub": f"sub-{user}",
        "username": user,
        "cognito:groups": ["analyst"] if groups is None else groups,
        "iss": ISS,
        "client_id": "app-client",
        "exp": int(time.time()) + exp_in,
        **extra,
    }
    return jwt.encode(claims, secret, algorithm=alg)


# ---- TokenVerifier


async def test_valid_token_yields_principal() -> None:
    p = await TokenVerifier(CFG).verify(token(groups=["analyst", "other"]))
    assert (p.username, p.subject, p.roles) == ("ana", "sub-ana", {"analyst"})


@pytest.mark.parametrize(
    "bad",
    [
        token(exp_in=-600),  # expired
        token(secret="y" * 40),  # wrong signature
        token(iss="https://evil.test"),  # wrong issuer
        token(client_id="other-app"),  # wrong audience
        "not-a-token",
    ],
)
async def test_bad_tokens_are_rejected(bad: str) -> None:
    with pytest.raises(AuthError):
        await TokenVerifier(CFG).verify(bad)


async def test_alg_none_is_rejected() -> None:
    forged = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, "", "none")
    with pytest.raises(AuthError):
        await TokenVerifier(CFG).verify(forged)


async def test_aud_claim_also_satisfies_audience() -> None:
    t = token(aud="app-client", client_id=None)
    assert (await TokenVerifier(CFG).verify(t)).username == "ana"


async def test_unknown_groups_give_no_roles() -> None:
    p = await TokenVerifier(CFG).verify(token(groups=["admins"]))
    assert p.roles == frozenset()


async def test_rs256_with_resolved_key() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = TokenVerifier(
        VerifierConfig(issuer=ISS), key_resolver=lambda _t: key.public_key()
    )
    t = jwt.encode(
        {
            "sub": "s",
            "exp": int(time.time()) + 60,
            "iss": ISS,
            "cognito:groups": ["supervisor"],
        },
        key,
        algorithm="RS256",
    )
    assert (await v.verify(t)).roles == {"supervisor"}
    # an HS256 token signed with the public key must not pass (algorithm confusion)
    with pytest.raises(AuthError):
        await v.verify(token(secret="z" * 40))


def test_verifier_needs_a_key_source() -> None:
    with pytest.raises(ValueError):
        TokenVerifier(VerifierConfig())


# ---- API


@pytest.fixture
def parts() -> tuple[FakeStore, FakeQueue]:
    return FakeStore({1, 2}), FakeQueue()


@pytest.fixture
async def client(
    parts: tuple[FakeStore, FakeQueue],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(store=parts[0], queue=parts[1], verifier=TokenVerifier(CFG))  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


def bearer(**kw: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(**kw)}"}


async def test_public_endpoints_need_no_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/alerts/1/investigations"),
        ("GET", "/cases/101"),
        ("GET", "/alerts/1/case"),
        ("GET", "/cases/101/events"),
        ("POST", "/cases/101/decision"),
    ],
)
async def test_protected_routes_reject_missing_token(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    r = await client.request(method, path, json={"decision": "approve"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


async def test_expired_token_is_401(client: httpx.AsyncClient) -> None:
    r = await client.post("/alerts/1/investigations", headers=bearer(exp_in=-60))
    assert r.status_code == 401


async def test_token_without_a_known_role_is_403(client: httpx.AsyncClient) -> None:
    r = await client.post("/alerts/1/investigations", headers=bearer(groups=[]))
    assert r.status_code == 403


async def test_analyst_can_start_and_read(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    h = bearer()
    assert (await client.post("/alerts/1/investigations", headers=h)).status_code == 202
    assert (await client.get("/cases/101", headers=h)).status_code == 200


def review(parts: tuple[FakeStore, FakeQueue], risk: float | None) -> None:
    parts[0].cases[1] = {
        "case_id": 101,
        "alert_id": 1,
        "status": "awaiting_review",
        "verdict": None if risk is None else {"risk_score": risk},
    }


async def test_decision_is_attributed_to_the_token_user(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    review(parts, 0.5)
    r = await client.post(
        "/cases/101/decision", json={"decision": "approve"}, headers=bearer(user="ana")
    )
    assert r.status_code == 202
    assert parts[0].decisions[101]["analyst_id"] == "ana"
    assert parts[0].decisions[101]["audit_extra"] == {
        "roles": ["analyst"],
        "subject": "sub-ana",
    }


async def test_body_analyst_id_must_match_the_token(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    review(parts, 0.5)
    body = {"decision": "approve", "analyst_id": "mallory"}
    r = await client.post("/cases/101/decision", json=body, headers=bearer(user="ana"))
    assert r.status_code == 403 and 101 not in parts[0].decisions


async def test_high_risk_case_needs_a_supervisor(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    review(parts, 0.95)
    body = {"decision": "approve"}
    r = await client.post("/cases/101/decision", json=body, headers=bearer())
    assert r.status_code == 403 and 101 not in parts[0].decisions
    r = await client.post(
        "/cases/101/decision",
        json=body,
        headers=bearer(user="sam", groups=["supervisor"]),
    )
    assert r.status_code == 202
    assert parts[0].decisions[101]["analyst_id"] == "sam"


async def test_analyst_can_decide_below_the_threshold(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    review(parts, 0.89)
    r = await client.post(
        "/cases/101/decision", json={"decision": "reject"}, headers=bearer()
    )
    assert r.status_code == 202


# ---- auth off


async def test_auth_off_keeps_working_and_needs_analyst_id() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    app = create_app(store=store, queue=queue)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        assert (await c.post("/alerts/1/investigations")).status_code == 202
        store.cases[1]["status"] = "awaiting_review"
        r = await c.post("/cases/101/decision", json={"decision": "approve"})
        assert r.status_code == 422  # no token, so the caller must say who they are
        r = await c.post(
            "/cases/101/decision", json={"decision": "approve", "analyst_id": "ana"}
        )
        assert r.status_code == 202
