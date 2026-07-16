"""SQLite: связь Telegram-пользователь → его заявки.

Лежит на примонтированном volume, поэтому переживает пересоздание контейнера.
sqlite3 из стандартной библиотеки синхронный, поэтому каждый вызов уходит в отдельный
поток — event loop не блокируется.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

from .ticket import Ticket

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                TEXT PRIMARY KEY,
    telegram_user_id  INTEGER NOT NULL,
    telegram_username TEXT,
    planner_task_id   TEXT,
    department        TEXT,
    task_type         TEXT,
    summary           TEXT,
    deadline          TEXT,
    priority          TEXT,
    created_at        TEXT,
    created_ts        TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests (telegram_user_id, created_ts DESC);
"""


class RequestsDB:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # --- инициализация ------------------------------------------------------

    def _init_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)
        log.info("База заявок готова: %s", self.path)

    # --- запись -------------------------------------------------------------

    def _add_sync(self, ticket: Ticket, created_ts: str) -> None:
        with self._connect() as conn:
            # INSERT OR IGNORE — на одну заявку ровно одна строка
            conn.execute(
                """
                INSERT OR IGNORE INTO requests (
                    id, telegram_user_id, telegram_username, planner_task_id,
                    department, task_type, summary, deadline, priority, created_at, created_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.number,
                    ticket.tg_user_id,
                    ticket.tg_username,
                    ticket.planner_task_id,
                    ticket.department,
                    ticket.task_type,
                    ticket.effective_description,
                    ticket.deadline,
                    ticket.priority,
                    ticket.created_at,
                    created_ts,
                ),
            )

    async def add(self, ticket: Ticket, created_ts: str) -> None:
        try:
            await asyncio.to_thread(self._add_sync, ticket, created_ts)
            log.info("Заявка %s записана в базу", ticket.number)
        except sqlite3.Error as exc:
            # заявка уже в Planner и в jsonl — из-за базы её не теряем
            log.error("Не удалось записать заявку %s в базу: %s", ticket.number, exc)

    # --- чтение -------------------------------------------------------------

    def _list_sync(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE telegram_user_id = ? ORDER BY created_ts DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_for_user(self, user_id: int) -> list[dict]:
        try:
            return await asyncio.to_thread(self._list_sync, user_id)
        except sqlite3.Error as exc:
            log.error("Не удалось прочитать заявки пользователя %s: %s", user_id, exc)
            return []

    def _get_sync(self, request_id: str, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ? AND telegram_user_id = ?",
                (request_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    async def get(self, request_id: str, user_id: int) -> dict | None:
        """Заявку отдаём только её автору — id из callback_data доверять нельзя."""
        try:
            return await asyncio.to_thread(self._get_sync, request_id, user_id)
        except sqlite3.Error as exc:
            log.error("Не удалось прочитать заявку %s: %s", request_id, exc)
            return None
