"""Локальное логирование заявок в jsonl — страховка независимо от Planner."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .ticket import Ticket

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


def _append_sync(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _append(path: Path, record: dict) -> None:
    record = {"logged_at": datetime.now(timezone.utc).isoformat(), **record}
    async with _lock:
        try:
            # запись в файл блокирующая — уводим в тред, чтобы не держать event loop
            await asyncio.to_thread(_append_sync, path, record)
        except OSError as exc:
            log.error("Не удалось записать в %s: %s", path, exc)


async def log_request(path: Path, ticket: Ticket, status: str) -> None:
    """Пишет каждую отправленную заявку — независимо от результата Planner."""
    await _append(path, {"status": status, **ticket.to_dict()})


async def log_fallback(path: Path, ticket: Ticket, error: str) -> None:
    """Заявки, которые не удалось создать в Planner — чтобы не потерять."""
    await _append(path, {"error": error, **ticket.to_dict()})
    log.warning("Заявка %s записана в fallback-лог %s", ticket.number, path)
