"""SQLite: связь Telegram-пользователь → его заявки.

Лежит на примонтированном volume, поэтому переживает пересоздание контейнера.
sqlite3 из стандартной библиотеки синхронный, поэтому каждый вызов уходит в отдельный
поток — event loop не блокируется.
"""

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timedelta
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
    contact           TEXT,
    created_at        TEXT,
    created_ts        TEXT,
    attachments_count INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'new',
    status_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests (telegram_user_id, created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests (created_ts DESC);
"""

# Колонки, добавленные после первого релиза. Для существующих баз мягко доливаем их
# при старте — у SQLite нет ADD COLUMN IF NOT EXISTS, поэтому сверяемся с PRAGMA.
MIGRATIONS: dict[str, str] = {
    "contact": "ALTER TABLE requests ADD COLUMN contact TEXT",
    "attachments_count": "ALTER TABLE requests ADD COLUMN attachments_count INTEGER DEFAULT 0",
    "status": "ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'new'",
    "status_updated_at": "ALTER TABLE requests ADD COLUMN status_updated_at TEXT",
}

# priority хранится по-русски; API оперирует ключами urgent/normal/low
PRIORITY_TO_KEY = {"Срочно": "urgent", "Обычный": "normal", "Не срочно": "low"}
PRIORITY_FROM_KEY = {v: k for k, v in PRIORITY_TO_KEY.items()}


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
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
            for column, ddl in MIGRATIONS.items():
                if column not in existing:
                    conn.execute(ddl)
                    log.info("Миграция БД: добавлена колонка %s", column)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)
        log.info("База заявок готова: %s", self.path)

    # --- запись -------------------------------------------------------------

    def _add_sync(self, ticket: Ticket, created_ts: str) -> None:
        with self._connect() as conn:
            # INSERT OR IGNORE — на одну заявку ровно одна строка.
            # status='new' по умолчанию: свежая заявка ещё не в работе (percentComplete=0)
            conn.execute(
                """
                INSERT OR IGNORE INTO requests (
                    id, telegram_user_id, telegram_username, planner_task_id,
                    department, task_type, summary, deadline, priority, contact,
                    created_at, created_ts, attachments_count, status, status_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
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
                    ticket.contact,
                    ticket.created_at,
                    created_ts,
                    len(ticket.attachments),
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

    # --- статусный кеш (для HTTP API) --------------------------------------

    def _active_sync(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, planner_task_id FROM requests "
                "WHERE planner_task_id IS NOT NULL AND planner_task_id != '' "
                "AND status != 'done'"
            ).fetchall()
        return [dict(r) for r in rows]

    async def active_tasks(self) -> list[dict]:
        """Заявки, чей статус ещё может измениться — их и обходит фоновый рефрешер."""
        try:
            return await asyncio.to_thread(self._active_sync)
        except sqlite3.Error as exc:
            log.error("Не удалось прочитать активные заявки: %s", exc)
            return []

    def _update_statuses_sync(self, updates: list[tuple[str, str, str]]) -> None:
        # updates: (status, status_updated_at, id)
        with self._connect() as conn:
            conn.executemany(
                "UPDATE requests SET status = ?, status_updated_at = ? WHERE id = ?", updates
            )

    async def update_statuses(self, updates: list[tuple[str, str, str]]) -> None:
        if not updates:
            return
        try:
            await asyncio.to_thread(self._update_statuses_sync, updates)
        except sqlite3.Error as exc:
            log.error("Не удалось обновить статусы (%d шт.): %s", len(updates), exc)

    # --- агрегаты и выборки (для HTTP API) ---------------------------------

    async def stats(self) -> dict:
        try:
            return await asyncio.to_thread(self._stats_sync)
        except sqlite3.Error as exc:
            log.error("Не удалось собрать статистику: %s", exc)
            return {}

    def _stats_sync(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
            today = conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE date(created_ts) = date('now','localtime')"
            ).fetchone()["c"]

            status_rows = conn.execute(
                "SELECT status, COUNT(*) c FROM requests GROUP BY status"
            ).fetchall()
            by_status = {"new": 0, "in_progress": 0, "done": 0}
            for r in status_rows:
                if r["status"] in by_status:
                    by_status[r["status"]] = r["c"]

            by_department = [
                {"name": r["department"] or "—", "count": r["c"]}
                for r in conn.execute(
                    "SELECT department, COUNT(*) c FROM requests "
                    "GROUP BY department ORDER BY c DESC"
                ).fetchall()
            ]
            by_type = [
                {"name": r["task_type"] or "—", "count": r["c"]}
                for r in conn.execute(
                    "SELECT task_type, COUNT(*) c FROM requests "
                    "GROUP BY task_type ORDER BY c DESC"
                ).fetchall()
            ]

            prio_rows = conn.execute(
                "SELECT priority, COUNT(*) c FROM requests GROUP BY priority"
            ).fetchall()
            by_priority = {"urgent": 0, "normal": 0, "low": 0}
            for r in prio_rows:
                key = PRIORITY_TO_KEY.get(r["priority"])
                if key:
                    by_priority[key] += r["c"]

            # динамика за 30 дней: заполняем нулями пропущенные дни для ровного графика
            day_rows = conn.execute(
                "SELECT date(created_ts) d, COUNT(*) c FROM requests "
                "WHERE date(created_ts) >= date('now','localtime','-29 days') "
                "GROUP BY d"
            ).fetchall()
            counts = {r["d"]: r["c"] for r in day_rows if r["d"]}
            base = datetime.now().date()
            by_day = [
                {
                    "date": (day := (base - timedelta(days=29 - i)).isoformat()),
                    "count": counts.get(day, 0),
                }
                for i in range(30)
            ]

            first = conn.execute(
                "SELECT date(MIN(created_ts)) d FROM requests"
            ).fetchone()["d"]

        if total and first:
            span_days = (datetime.now().date() - date.fromisoformat(first)).days + 1
            avg_per_day = round(total / max(span_days, 1), 2)
        else:
            avg_per_day = 0.0

        return {
            "total": total,
            "today": today,
            "by_status": by_status,
            "by_department": by_department,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_day": by_day,
            "avg_per_day": avg_per_day,
        }

    async def list_tickets(self, filters: dict, page: int, per_page: int) -> dict:
        try:
            return await asyncio.to_thread(self._list_tickets_sync, filters, page, per_page)
        except sqlite3.Error as exc:
            log.error("Не удалось выбрать заявки: %s", exc)
            return {"total": 0, "page": page, "per_page": per_page, "items": []}

    def _list_tickets_sync(self, filters: dict, page: int, per_page: int) -> dict:
        where: list[str] = []
        params: list[object] = []

        if filters.get("department"):
            where.append("department = ?")
            params.append(filters["department"])
        if filters.get("type"):
            where.append("task_type = ?")
            params.append(filters["type"])
        if filters.get("status"):
            where.append("status = ?")
            params.append(filters["status"])
        if filters.get("priority"):
            # принимаем и ключ urgent/normal/low, и русский текст
            value = PRIORITY_FROM_KEY.get(filters["priority"], filters["priority"])
            where.append("priority = ?")
            params.append(value)
        if filters.get("date_from"):
            where.append("date(created_ts) >= date(?)")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            where.append("date(created_ts) <= date(?)")
            params.append(filters["date_to"])
        if filters.get("q"):
            where.append("(summary LIKE ? OR contact LIKE ? OR id LIKE ?)")
            like = f"%{filters['q']}%"
            params += [like, like, like]

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        offset = (page - 1) * per_page

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) c FROM requests {clause}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM requests {clause} ORDER BY created_ts DESC LIMIT ? OFFSET ?",
                [*params, per_page, offset],
            ).fetchall()

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [dict(r) for r in rows],
        }
