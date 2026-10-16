# -*- coding: utf-8 -*-
"""Tests for the optional fastapi-guard middleware (GUARD_ENABLED, default off)."""

import asyncio
import itertools
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from api.app import create_app

GUARD_ENV_KEYS = (
    "GUARD_ENABLED",
    "GUARD_RATE_LIMIT",
    "GUARD_RATE_LIMIT_WINDOW",
    "GUARD_WHITELIST",
    "GUARD_BLACKLIST",
    "GUARD_TRUSTED_PROXIES",
    "GUARD_SCAN_BODY",
    "GUARD_PASSIVE_MODE",
    "TRUST_X_FORWARDED_FOR",
    "CORS_ALLOW_ALL",
)
ORIGIN = "http://localhost:5173"
_PEERS = itertools.count(1)


class _PeerClient:
    """Tiny sync client that fixes the TCP peer address seen by the app."""

    def __init__(self, app, peer):
        self._app = app
        self._peer = peer

    def request(self, method, path, **kwargs):
        async def _call():
            transport = httpx.ASGITransport(app=self._app, client=(self._peer, 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(_call())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


class SecurityGuardTestCase(unittest.TestCase):
    """The guard is opt-in and must not change the app when it is off."""

    def _build_app(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in GUARD_ENV_KEYS}
        clean.update(env)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        with patch.dict(os.environ, clean, clear=True):
            return create_app(static_dir=Path(temp_dir.name))

    def setUp(self):
        # guard-core keeps rate-limit counters in process-wide singletons, so every
        # test gets its own TCP peer address instead of sharing one bucket.
        self.peer = f"203.0.113.{next(_PEERS)}"

    def _client(self, app, peer=None):
        return _PeerClient(app, peer or self.peer)

    @staticmethod
    def _guard_cls():
        from guard import PureASGISecurityMiddleware

        return PureASGISecurityMiddleware

    def test_disabled_adds_nothing_and_never_imports_guard(self):
        sys.modules.pop("guard", None)
        app = self._build_app()

        self.assertNotIn("guard", sys.modules)
        self.assertEqual(
            [m.cls.__name__ for m in app.user_middleware],
            ["AuthMiddleware", "CORSMiddleware"],
        )

    def test_enabled_without_package_raises_clear_error(self):
        with patch.dict(sys.modules, {"guard": None}):
            with self.assertRaises(RuntimeError) as ctx:
                self._build_app(GUARD_ENABLED="true")
        self.assertIn("fastapi-guard", str(ctx.exception))

    def test_cors_wraps_guard_and_guard_wraps_auth(self):
        app = self._build_app(GUARD_ENABLED="true")

        order = [m.cls for m in app.user_middleware]
        self.assertEqual(order[0], CORSMiddleware)
        self.assertEqual(order[1], self._guard_cls())
        self.assertFalse(issubclass(order[1], BaseHTTPMiddleware))  # streaming responses stay unbuffered
        self.assertEqual(order[2].__name__, "AuthMiddleware")

    def test_rate_limit_burst_returns_429_with_cors_headers(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_RATE_LIMIT="3", GUARD_RATE_LIMIT_WINDOW="60")
        client = self._client(app)

        codes = [client.get("/api/v1/auth/status", headers={"Origin": ORIGIN}).status_code for _ in range(5)]
        blocked = client.get("/api/v1/auth/status", headers={"Origin": ORIGIN})

        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])
        self.assertEqual(blocked.headers.get("access-control-allow-origin"), ORIGIN)

    def test_allowlist_blocks_other_ips_with_403(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_WHITELIST="198.51.100.0/24")

        self.assertEqual(self._client(app, "203.0.113.250").get("/api/v1/auth/status").status_code, 403)
        self.assertEqual(self._client(app, "198.51.100.9").get("/api/v1/auth/status").status_code, 200)

    def test_allowlisted_ip_is_exempt_from_rate_limit(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_RATE_LIMIT="2", GUARD_WHITELIST=self.peer)
        client = self._client(app)

        self.assertEqual([client.get("/api/v1/auth/status").status_code for _ in range(5)], [200] * 5)

    def test_spoofed_xff_is_ignored_from_untrusted_peer(self):
        app = self._build_app(
            GUARD_ENABLED="true",
            TRUST_X_FORWARDED_FOR="true",
            GUARD_WHITELIST="198.51.100.9",
        )
        # Untrusted peer claims to be the allowlisted address.
        spoofed = self._client(app, "203.0.113.250").get(
            "/api/v1/auth/status", headers={"X-Forwarded-For": "198.51.100.9"}
        )
        self.assertEqual(spoofed.status_code, 403)

    def test_spoofed_xff_cannot_rotate_rate_limit_bucket(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_RATE_LIMIT="2", TRUST_X_FORWARDED_FOR="true")
        client = self._client(app, "203.0.113.250")

        codes = [
            client.get("/api/v1/auth/status", headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
            for i in range(4)
        ]
        self.assertEqual(codes, [200, 200, 429, 429])

    def test_trusted_proxy_uses_rightmost_xff_like_get_client_ip(self):
        app = self._build_app(
            GUARD_ENABLED="true",
            TRUST_X_FORWARDED_FOR="true",
            GUARD_WHITELIST="198.51.100.9",
        )
        client = self._client(app, "127.0.0.1")

        ok = client.get("/api/v1/auth/status", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.9"})
        bad = client.get("/api/v1/auth/status", headers={"X-Forwarded-For": "198.51.100.9, 6.6.6.6"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 403)

    def test_xff_ignored_when_trust_flag_is_off(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_WHITELIST="198.51.100.9")
        client = self._client(app, "127.0.0.1")

        resp = client.get("/api/v1/auth/status", headers={"X-Forwarded-For": "198.51.100.9"})
        self.assertEqual(resp.status_code, 403)

    def test_attack_in_query_is_blocked(self):
        app = self._build_app(GUARD_ENABLED="true")
        client = self._client(app)

        resp = client.get("/api/v1/auth/status", params={"q": "<script>alert(1)</script>"})
        self.assertEqual(resp.status_code, 400)

    def test_body_is_not_scanned_by_default(self):
        app = self._build_app(GUARD_ENABLED="true")
        client = self._client(app)

        # The app (auth is not configured here) answers, so the guard let the payload through unread.
        resp = client.post("/api/v1/auth/login", json={"password": "<script>alert(1)</script>"})
        self.assertIn("auth_disabled", resp.text)

    def test_body_scan_can_be_enabled(self):
        app = self._build_app(GUARD_ENABLED="true", GUARD_SCAN_BODY="true")
        client = self._client(app)

        resp = client.post("/api/v1/auth/login", json={"password": "<script>alert(1)</script>"})
        self.assertNotIn("auth_disabled", resp.text)
        self.assertEqual(resp.status_code, 400)

    def test_invalid_integer_setting_fails_fast(self):
        with self.assertRaises(ValueError):
            self._build_app(GUARD_ENABLED="true", GUARD_RATE_LIMIT="many")


if __name__ == "__main__":
    unittest.main()
