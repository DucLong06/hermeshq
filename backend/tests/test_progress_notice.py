import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.services import agent_supervisor
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.schedule_notifications import enqueue_progress_notice


def _session(user, keys):
    session = MagicMock()
    session.get = AsyncMock(return_value=user)
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = keys
    session.execute = AsyncMock(return_value=resultado)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _key():
    return SimpleNamespace(
        id="k1",
        trust_sender_id=False,
        bound_user_id="u1",
        last_notification_poll_at=datetime.now(UTC) - timedelta(seconds=20),
    )


def _task(**metadata):
    base = {"source": "public_chat", "thread_user_id": "u1", "conversation": True}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", status="running", metadata_json=base)


class TestProgressNotice(unittest.IsolatedAsyncioTestCase):
    async def test_a_chat_task_gets_one_notice_per_live_bridge_key(self):
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [_key()])
        cantidad = await enqueue_progress_notice(sesion, _task(), "⏳ Sigo trabajando")
        self.assertEqual(cantidad, 1)
        aviso = sesion.add.call_args.args[0]
        self.assertEqual(aviso.body, "⏳ Sigo trabajando")
        self.assertEqual(aviso.task_id, "t1")
        self.assertIsNone(aviso.schedule_id)

    async def test_background_scheduled_and_anonymous_tasks_get_nothing(self):
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [_key()])
        for metadata in ({"background_job": True}, {"scheduled": True}, {"thread_user_id": ""}):
            self.assertEqual(await enqueue_progress_notice(sesion, _task(**metadata), "x"), 0)
        sesion.add.assert_not_called()

    async def test_without_a_live_bridge_nothing_is_queued(self):
        vieja = _key()
        vieja.last_notification_poll_at = datetime.now(UTC) - timedelta(hours=13)
        sesion = _session(SimpleNamespace(id="u1", is_active=True, teams_id=None), [vieja])
        self.assertEqual(await enqueue_progress_notice(sesion, _task(), "x"), 0)


class TestTimer(unittest.IsolatedAsyncioTestCase):
    def _supervisor(self, task):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        session = MagicMock()
        session.get = AsyncMock(return_value=task)
        session.commit = AsyncMock()
        contexto = MagicMock()
        contexto.__aenter__ = AsyncMock(return_value=session)
        contexto.__aexit__ = AsyncMock(return_value=False)
        supervisor.session_factory = MagicMock(return_value=contexto)
        return supervisor, session

    async def test_the_notice_is_sent_only_while_the_task_is_still_running(self):
        supervisor, session = self._supervisor(_task())
        with patch.object(agent_supervisor, "enqueue_progress_notice", AsyncMock(return_value=1)) as aviso:
            await supervisor._progress_notice_after("t1", 0)
        aviso.assert_awaited_once()
        self.assertIn("cancela", aviso.await_args.args[2])
        session.commit.assert_awaited_once()
        terminada = _task()
        terminada.status = "completed"
        supervisor, session = self._supervisor(terminada)
        with patch.object(agent_supervisor, "enqueue_progress_notice", AsyncMock(return_value=1)) as aviso:
            await supervisor._progress_notice_after("t1", 0)
        aviso.assert_not_awaited()

    def test_the_heads_up_comes_after_three_minutes(self):
        self.assertEqual(agent_supervisor.PROGRESS_NOTICE_SECONDS, 180)


if __name__ == "__main__":
    unittest.main()
