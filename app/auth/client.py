"""Auth header for scripts that call the API (smoke test, load test), F11."""

import os


def api_headers() -> dict[str, str]:
    """`Authorization: Bearer $API_TOKEN` when set (needed if the API runs AUTH_MODE=jwt)."""
    token = os.environ.get("API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}
