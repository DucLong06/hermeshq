import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.services.audit import extract_ip


def _request(host="10.0.0.9", forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


class TestExtractIp(unittest.TestCase):
    def test_a_forwarded_header_cannot_set_the_recorded_ip(self):
        self.assertEqual(extract_ip(_request(forwarded="6.6.6.6")), "10.0.0.9")

    def test_no_client_returns_none(self):
        self.assertIsNone(extract_ip(SimpleNamespace(headers={}, client=None)))


class TestFailedLoginAudit(unittest.IsolatedAsyncioTestCase):
    async def _intentar(self, usuario, existe):
        from hermeshq.routers.auth import helpers, local

        helpers._login_attempts.clear()
        resultado = MagicMock()
        resultado.scalar_one_or_none.return_value = SimpleNamespace(password_hash="x") if existe else None
        db = MagicMock()
        db.execute = AsyncMock(return_value=resultado)
        db.commit = AsyncMock()
        carga = SimpleNamespace(username=usuario, password="mala")
        with (
            patch.object(local, "verify_password", return_value=False),
            patch.object(local, "record_audit", new=AsyncMock()) as auditoria,
            self.assertRaises(HTTPException) as ctx,
        ):
            await local.login(carga, MagicMock(), _request(), db)
        return ctx.exception, auditoria, db

    async def test_a_failed_login_is_audited_with_username_and_ip(self):
        error, auditoria, db = await self._intentar("bridge-teams", existe=True)
        self.assertEqual(error.status_code, 401)
        datos = auditoria.await_args.kwargs
        self.assertEqual(datos["action"], "auth.login_failed")
        self.assertEqual(datos["actor_username"], "bridge-teams")
        self.assertEqual(datos["ip_address"], "10.0.0.9")
        self.assertEqual(datos["details"], {"user_exists": True})
        db.commit.assert_awaited()

    async def test_unknown_users_are_audited_and_long_names_are_cut(self):
        _, auditoria, _ = await self._intentar("x" * 300, existe=False)
        datos = auditoria.await_args.kwargs
        self.assertEqual(len(datos["actor_username"]), 64)
        self.assertEqual(datos["details"], {"user_exists": False})


class TestPerUserThrottle(unittest.IsolatedAsyncioTestCase):
    async def _login(self, usuario, existe, ip="10.0.0.9", correcta=False):
        from hermeshq.routers.auth import local

        resultado = MagicMock()
        resultado.scalar_one_or_none.return_value = (
            SimpleNamespace(id="u1", password_hash="x", role="user", email=None) if existe else None
        )
        db = MagicMock()
        db.execute = AsyncMock(return_value=resultado)
        db.commit = AsyncMock()
        carga = SimpleNamespace(username=usuario, password="p")
        respuesta = MagicMock()
        with (
            patch.object(local, "verify_password", return_value=correcta),
            patch.object(local, "record_audit", new=AsyncMock()),
            patch.object(local, "_is_mfa_globally_enabled", new=AsyncMock(return_value=False)),
            patch.object(local, "create_access_token", return_value=("tok", datetime.now(UTC))),
            patch.object(local, "_set_auth_cookie"),
        ):
            return await local.login(carga, respuesta, _request(host=ip), db)

    def setUp(self):
        from hermeshq.routers.auth import helpers

        helpers._login_attempts.clear()

    async def _fallar(self, veces, usuario="admin", existe=True, ip="10.0.0.9"):
        for _ in range(veces):
            with self.assertRaises(HTTPException) as ctx:
                await self._login(usuario, existe, ip)
            self.assertEqual(ctx.exception.status_code, 401)

    async def test_guessing_one_account_locks_only_that_account(self):
        await self._fallar(8)
        with self.assertRaises(HTTPException) as bloqueado:
            await self._login("admin", True)
        self.assertEqual(bloqueado.exception.status_code, 429)
        with self.assertRaises(HTTPException) as otro:
            await self._login("maria", True)
        self.assertEqual(otro.exception.status_code, 401)

    async def test_a_shared_proxy_ip_is_not_locked_by_ten_failures(self):
        await self._fallar(9, usuario="a", existe=False)
        await self._fallar(3, usuario="b", existe=False)
        with self.assertRaises(HTTPException) as ctx:
            await self._login("c", False)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_a_flood_from_one_ip_is_still_blocked(self):
        await self._fallar(40, usuario="x", existe=False)
        with self.assertRaises(HTTPException) as ctx:
            await self._login("y", False)
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_unknown_names_do_not_create_per_user_entries(self):
        from hermeshq.routers.auth import helpers

        await self._fallar(5, usuario="fail3d", existe=False)
        self.assertNotIn("user:fail3d", helpers._login_attempts)

    async def test_a_good_login_clears_the_failed_counter_of_the_account(self):
        from hermeshq.routers.auth import helpers

        await self._fallar(5)
        await self._login("admin", True, correcta=True)
        self.assertNotIn("user:admin", helpers._login_attempts)

    async def test_names_are_matched_without_case_or_spaces(self):
        await self._fallar(4, usuario="Admin")
        await self._fallar(4, usuario=" admin ")
        with self.assertRaises(HTTPException) as ctx:
            await self._login("ADMIN", True)
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
