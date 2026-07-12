"""In-memory fake Supabase (Auth + REST) for account/login tests.

Not a general PostgREST/GoTrue emulator -- just enough behavior to exercise
app/accounts.py and the auth/account routes end to end without a real network
call. One instance's `transport()` can back both a SupabaseAuthClient and a
SupabaseClient in the same test, since it's the same fake project.
"""

import json
from typing import Any

import httpx


class FakeSupabaseBackend:
    def __init__(self) -> None:
        self.users_by_code: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        # refresh_token -> user. Independently valid of whether the access
        # token it originally shipped with is still tracked in `sessions`,
        # same as a real refresh token.
        self.refreshable: dict[str, dict[str, Any]] = {}
        self.customers: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []
        self.otp_requests: list[dict[str, Any]] = []
        self._counter = 0

    def register_login_code(
        self,
        code: str,
        *,
        user_id: str,
        email: str | None,
        user_name: str | None = None,
        provider: str = "github",
    ) -> None:
        self.users_by_code[code] = {
            "id": user_id,
            "email": email,
            "user_metadata": {"user_name": user_name} if user_name else {},
            "app_metadata": {"provider": provider},
        }

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _mint_session(self, user: dict[str, Any]) -> dict[str, Any]:
        access_token = self._next_id("access")
        refresh_token = self._next_id("refresh")
        self.sessions[access_token] = user
        self.refreshable[refresh_token] = user
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "user": user,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/auth/v1/token" and request.url.params.get("grant_type") == "pkce":
            body = json.loads(request.content)
            user = self.users_by_code.get(body.get("auth_code", ""))
            if user is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._mint_session(user))

        if path == "/auth/v1/token" and request.url.params.get("grant_type") == "refresh_token":
            body = json.loads(request.content)
            user = self.refreshable.get(body.get("refresh_token", ""))
            if user is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._mint_session(user))

        if path == "/auth/v1/user":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            user = self.sessions.get(token)
            if user is None:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json=user)

        if path == "/auth/v1/logout":
            return httpx.Response(204)

        if path == "/auth/v1/otp" and method == "POST":
            body = json.loads(request.content)
            self.otp_requests.append(
                {
                    "email": body.get("email"),
                    "create_user": body.get("create_user"),
                    "code_challenge": body.get("code_challenge"),
                    "code_challenge_method": body.get("code_challenge_method"),
                    "redirect_to": request.url.params.get("redirect_to"),
                }
            )
            return httpx.Response(200, json={})

        if path == "/rest/v1/api_customers" and method == "GET":
            auth_user_id = request.url.params.get("auth_user_id", "").removeprefix("eq.")
            matches = [c for c in self.customers if c["auth_user_id"] == auth_user_id]
            return httpx.Response(200, json=matches[:1])

        if path == "/rest/v1/api_customers" and method == "POST":
            body = json.loads(request.content)
            record = {
                "id": self._next_id("customer"),
                "name": body["name"],
                "email": body.get("email"),
                "auth_user_id": body["auth_user_id"],
            }
            self.customers.append(record)
            return httpx.Response(201, json=[record])

        if path == "/rest/v1/api_keys" and method == "GET":
            customer_id = request.url.params.get("customer_id", "").removeprefix("eq.")
            matches = [k for k in self.keys if k["customer_id"] == customer_id]
            return httpx.Response(200, json=matches)

        if path == "/rest/v1/api_keys" and method == "POST":
            body = json.loads(request.content)
            record = {
                "id": self._next_id("key"),
                "customer_id": body["customer_id"],
                "prefix": body["prefix"],
                "secret_hash": body["secret_hash"],
                "label": body.get("label"),
                "scopes": body["scopes"],
                "daily_quota": body["daily_quota"],
                "revoked": False,
                "expires_at": None,
                "created_at": "2026-07-11T00:00:00+00:00",
                "last_used_at": None,
            }
            self.keys.append(record)
            return httpx.Response(201, json=[record])

        if path == "/rest/v1/api_keys" and method == "PATCH":
            key_id = request.url.params.get("id", "").removeprefix("eq.")
            customer_id = request.url.params.get("customer_id", "").removeprefix("eq.")
            matches = [
                k for k in self.keys if k["id"] == key_id and k["customer_id"] == customer_id
            ]
            for k in matches:
                k["revoked"] = True
            return httpx.Response(200, json=matches)

        if path == "/rest/v1/audit_events" and method == "POST":
            self.audit_events.append(json.loads(request.content))
            return httpx.Response(201, json=[])

        raise AssertionError(f"unexpected fake-Supabase request: {method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
