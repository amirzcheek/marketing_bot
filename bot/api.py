"""Лёгкий HTTP API со статистикой заявок для веб-панели.

Поднимается в том же asyncio-loop, что и long polling бота, слушает 0.0.0.0:API_PORT
внутри сети деплоя. Наружу порт не публикуется. Все эндпоинты, кроме /health, требуют
заголовок Authorization: Bearer <API_TOKEN>.

Статусы заявок не читаются из Planner на каждый запрос — их раз в STATUS_REFRESH_SECONDS
обновляет фоновая задача, а API отдаёт готовые значения из SQLite (быстро).
"""

import asyncio
import hmac
import json
import logging
from datetime import datetime

from aiohttp import web

from .config import Config
from .db import RequestsDB
from .graph import GraphError, PlannerClient
from .ticket import api_status

log = logging.getLogger(__name__)

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 20


def _json(data: object, status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False)
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)

    cfg: Config = request.app["config"]
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""

    # пустой API_TOKEN — доступ закрыт полностью (кроме /health)
    if not cfg.api_token or not hmac.compare_digest(token, cfg.api_token):
        return _json({"error": "unauthorized"}, status=401)
    return await handler(request)


# --- эндпоинты --------------------------------------------------------------


async def health(request: web.Request) -> web.Response:
    return _json({"status": "ok"})


async def stats(request: web.Request) -> web.Response:
    db: RequestsDB = request.app["db"]
    return _json(await db.stats())


def _planner_url(cfg: Config, task_id: str | None) -> str:
    if not task_id:
        return ""
    return f"https://tasks.office.com/{cfg.graph_tenant_id}/Home/Task/{task_id}"


def _int_param(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


async def tickets(request: web.Request) -> web.Response:
    db: RequestsDB = request.app["db"]
    cfg: Config = request.app["config"]

    filters = {
        key: request.query.get(key, "").strip()
        for key in ("department", "type", "status", "priority", "date_from", "date_to", "q")
    }
    page = _int_param(request, "page", 1, 1, 10_000_000)
    per_page = _int_param(request, "per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)

    result = await db.list_tickets(filters, page, per_page)
    items = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "department": r["department"],
            "task_type": r["task_type"],
            "summary": r["summary"],
            "deadline": r["deadline"],
            "priority": r["priority"],
            "status": r["status"] or "new",
            "contact": r["contact"],
            "planner_task_id": r["planner_task_id"],
            "planner_url": _planner_url(cfg, r["planner_task_id"]),
            "attachments_count": r["attachments_count"] or 0,
        }
        for r in result["items"]
    ]
    return _json({**result, "items": items})


# --- фоновый рефрешер статусов ---------------------------------------------


async def _refresh_once(app: web.Application) -> None:
    db: RequestsDB = app["db"]
    planner: PlannerClient | None = app["planner"]
    if planner is None:
        return

    active = await db.active_tasks()
    if not active:
        return

    # одним запросом забираем все задачи плана вместо N штук по одной
    tasks = await planner.list_plan_tasks()
    now = datetime.now().isoformat(timespec="seconds")
    updates: list[tuple[str, str, str]] = []
    for row in active:
        task = tasks.get(row["planner_task_id"])
        if task is None:
            continue  # задача удалена из плана — сохраняем последний известный статус
        updates.append((api_status(task.get("percentComplete")), now, row["id"]))

    await db.update_statuses(updates)
    if updates:
        log.info("Статусы заявок обновлены: %d", len(updates))


async def status_refresher(app: web.Application) -> None:
    cfg: Config = app["config"]
    while True:
        try:
            await _refresh_once(app)
        except GraphError as exc:
            # Planner недоступен — не страшно, БД отдаёт последний известный статус
            log.warning("Рефрешер статусов: Planner недоступен: %s", exc)
        except Exception:
            log.exception("Рефрешер статусов: неожиданная ошибка")
        await asyncio.sleep(cfg.status_refresh_seconds)


# --- запуск/остановка вместе с ботом ---------------------------------------


def build_app(cfg: Config, db: RequestsDB, planner: PlannerClient | None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["config"] = cfg
    app["db"] = db
    app["planner"] = planner
    app.router.add_get("/health", health)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/tickets", tickets)
    return app


class ApiServer:
    """Держит AppRunner и фоновую задачу; поднимается в loop'е бота."""

    def __init__(self, cfg: Config, db: RequestsDB, planner: PlannerClient | None):
        self.cfg = cfg
        self.app = build_app(cfg, db, planner)
        self._runner: web.AppRunner | None = None
        self._refresher: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.cfg.api_token:
            log.warning(
                "API_TOKEN не задан — HTTP API поднимется, но все запросы (кроме /health) "
                "будут отклоняться с 401. Задай API_TOKEN в .env."
            )
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.cfg.api_port)
        await site.start()
        self._refresher = asyncio.create_task(status_refresher(self.app))
        log.info("HTTP API слушает 0.0.0.0:%s (рефреш статусов раз в %d c)",
                 self.cfg.api_port, self.cfg.status_refresh_seconds)

    async def stop(self) -> None:
        if self._refresher:
            self._refresher.cancel()
            try:
                await self._refresher
            except asyncio.CancelledError:
                pass
        if self._runner:
            await self._runner.cleanup()
        log.info("HTTP API остановлен")
