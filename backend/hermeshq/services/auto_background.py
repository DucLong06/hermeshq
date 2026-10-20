import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import can_access_agent
from hermeshq.models.activity import ActivityLog
from hermeshq.models.public_chat import PublicChatMessage
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, current_time_context
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)

TIMEOUT_MARKER = "exceeded the task timeout"
HISTORY_MESSAGES = 6
HISTORY_CHARS_PER_MESSAGE = 700
MAX_TITLE_CHARS = 120
AUTO_MARKER = "auto_background_job_id"


def is_continuable(task, error: str) -> bool:
    metadata = getattr(task, "metadata_json", None) or {}
    if metadata.get("source") != "public_chat" or metadata.get("background_job") or metadata.get("scheduled"):
        return False
    if not metadata.get("thread_user_id") or metadata.get("public_chat_binding_denied"):
        return False
    return TIMEOUT_MARKER in (error or "") and bool((getattr(task, "prompt", "") or "").strip())


ATTACHMENT_NOTE = re.compile(r"^\s*\[Archivos adjuntos.*?\]\s*", re.DOTALL)


def _title_from(prompt: str) -> str:
    text = ATTACHMENT_NOTE.sub("", prompt or "", count=1).strip()
    return _clip(text or "solicitud con archivos adjuntos", MAX_TITLE_CHARS)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


SYSTEM_REPLY_PREFIXES = ("⚠️ Mi intento anterior", "⏳", "⏹️", "🧹")
LOOKBACK_MESSAGES = 40
SUBSTANTIAL_REQUEST_CHARS = 80


def _is_system_reply(message) -> bool:
    return message.role != "user" and (message.content or "").lstrip().startswith(SYSTEM_REPLY_PREFIXES)


async def _recent_history(session: AsyncSession, session_id: str | None) -> str:
    if not session_id:
        return ""
    result = await session.execute(
        select(PublicChatMessage)
        .where(PublicChatMessage.session_id == session_id)
        .order_by(PublicChatMessage.created_at.desc())
        .limit(LOOKBACK_MESSAGES)
    )
    newest_first = [m for m in result.scalars().all() if not _is_system_reply(m)]
    recent = newest_first[:HISTORY_MESSAGES]
    request = next(
        (
            m
            for m in newest_first[HISTORY_MESSAGES:]
            if m.role == "user" and len((m.content or "").strip()) >= SUBSTANTIAL_REQUEST_CHARS
        ),
        None,
    )
    lines = []
    if request is not None:
        lines.append(f"Pedido de fondo anterior del usuario: {_clip(request.content, HISTORY_CHARS_PER_MESSAGE)}")
    for message in reversed(recent):
        who = "Usuario" if message.role == "user" else "Tú"
        lines.append(f"{who}: {_clip(message.content, HISTORY_CHARS_PER_MESSAGE)}")
    return "\n".join(lines)


async def create_continuation(session: AsyncSession, task, error: str) -> Task | None:
    if not is_continuable(task, error):
        return None
    metadata = task.metadata_json or {}
    user_id = str(metadata["thread_user_id"])
    user = await session.get(User, user_id)
    if not user or not user.is_active or not await can_access_agent(session, user, task.agent_id):
        return None
    active = await session.execute(
        select(Task).where(Task.agent_id == task.agent_id, Task.status.in_(("queued", "running")))
    )
    for item in active.scalars().all():
        item_metadata = item.metadata_json or {}
        if item_metadata.get("background_job") and item_metadata.get("thread_user_id") == user_id:
            return None
    timeout_minutes = int(get_settings().background_job_timeout_seconds) // 60
    title = _title_from(task.prompt)
    history = await _recent_history(session, metadata.get("public_session_id"))
    context = f"Conversación reciente:\n{history}\n\n" if history else ""
    prompt = (
        f"{context}Último mensaje del usuario, que debes resolver:\n{task.prompt.strip()}\n\n"
        "[CONTINUACIÓN AUTOMÁTICA EN SEGUNDO PLANO: tu intento anterior en el chat agotó el tiempo y se cortó. "
        "El usuario no ve este trabajo ni puedes preguntarle nada. Antes de empezar revisa tu workspace "
        "(work/) y reutiliza lo que ya quedó hecho: no repitas descargas ni extracciones. "
        "Usa las herramientas del catálogo de capacidades en vez de escribir scripts a medida. Entrega al final el "
        "resultado COMPLETO y listo para leer (conclusión primero, cifras exactas de la fuente, sin inventar). Si "
        "generas un archivo, guárdalo en tu espacio de trabajo y da la ruta. "
        f"Tienes hasta {timeout_minutes} minutos.]\n\n{current_time_context(DEFAULT_TIMEZONE)}"
    )
    job = Task(
        agent_id=task.agent_id,
        created_by_user_id=None,
        title=f"Trabajo en segundo plano: {title}"[:255],
        prompt=prompt,
        metadata_json={
            "background_job": True,
            "background_title": title,
            "source": "background_job",
            "thread_user_id": user_id,
            "origin_task_id": task.id,
            "auto_continuation": True,
        },
    )
    job.board_column = runtime_status_to_board_column(job.status)
    job.board_order = next_board_order()
    job.board_manual = False
    session.add(job)
    await session.flush()
    task.metadata_json = {**metadata, AUTO_MARKER: job.id}
    session.add(
        ActivityLog(
            agent_id=task.agent_id,
            task_id=task.id,
            event_type="background_job.auto_started",
            message=title,
            details={"requested_for_user_id": user_id, "reason": "chat_timeout"},
        )
    )
    return job
