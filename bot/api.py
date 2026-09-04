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
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .config import Config
from .constants import DEPARTMENTS, NO_DEADLINE, PRIORITIES, REQUEST_TYPES, TASK_TYPE_CATEGORY
from .db import RequestsDB
from .departments import DepartmentRegistry
from .graph import GraphError, PlannerClient, sanitize_filename
from .router import TicketRouter
from .storage import log_fallback, log_request
from .ticket import (
    Attachment,
    Ticket,
    api_status,
    new_number,
    notification_html,
    planner_description,
    planner_title,
)

log = logging.getLogger(__name__)

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 20
MAX_WEB_FILE_SIZE = 20 * 1024 * 1024
MAX_WEB_REQUEST_SIZE = 100 * 1024 * 1024
WEB_EMAIL_DOMAIN = "@knus.edu.kz"


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
    # необязательный разрез по отделу-исполнителю (department_target или target_department)
    target = (
        request.query.get("department_target") or request.query.get("target_department") or ""
    ).strip()
    return _json(await db.stats(target))


async def get_registry(request: web.Request) -> web.Response:
    """Список включённых отделов и их типы обращений — для наполнения веб-формы."""
    reg: DepartmentRegistry | None = request.app.get("registry")
    departments = []
    if reg is not None:
        for dep in reg.enabled():
            types = [
                {"code": key, "label": REQUEST_TYPES[key][0]}
                for key in dep.request_types
                if key in REQUEST_TYPES
            ]
            departments.append({"code": dep.code, "name": dep.name, "request_types": types})
    return _json({"departments": departments})


async def catalog(request: web.Request) -> web.Response:
    """Полный справочник формы: отделы+типы, подразделения-заявители, приоритеты, лимиты.

    Внешние клиенты не должны хардкодить эти справочники. Богаче, чем /api/registry.
    """
    registry: DepartmentRegistry | None = request.app.get("registry")
    if registry is None:
        return _json({"error": "registry_unavailable"}, status=503)

    targets = []
    for department in registry.enabled():
        request_types = []
        for key in department.request_types:
            value = REQUEST_TYPES.get(key)
            if value:
                request_types.append({"code": key, "name": value[1] or value[0]})
        targets.append(
            {"code": department.code, "name": department.name, "request_types": request_types}
        )

    requester_departments = [
        {"code": key, "name": value[1] or value[0]} for key, value in DEPARTMENTS.items()
    ]
    priorities = [{"code": key, "name": value[1]} for key, value in PRIORITIES.items()]
    cfg: Config = request.app["config"]
    return _json(
        {
            "version": "1.0",
            "departments": targets,
            "requester_departments": requester_departments,
            "priorities": priorities,
            "limits": {
                "max_attachments": cfg.max_attachments,
                "max_file_size_bytes": MAX_WEB_FILE_SIZE,
                "max_request_size_bytes": MAX_WEB_REQUEST_SIZE,
                "description_min_length": 10,
                "description_max_length": 5000,
            },
        }
    )


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
        for key in (
            "target_department", "department", "type", "status",
            "priority", "date_from", "date_to", "q",
        )
    }
    # синоним department_target → target_department
    if not filters["target_department"]:
        filters["target_department"] = request.query.get("department_target", "").strip()
    page = _int_param(request, "page", 1, 1, 10_000_000)
    per_page = _int_param(request, "per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)

    result = await db.list_tickets(filters, page, per_page)
    items = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "target_department": r["target_department"] or "marketing",
            "department": r["department"],  # подразделение заявителя
            "request_type": r["request_type"],
            "task_type": r["task_type"],
            "summary": r["summary"],
            "deadline": r["deadline"],
            "priority": r["priority"],
            "status": r["status"] or "new",
            "contact": r["contact"],
            "phone": r["phone"] or "",
            "planner_task_id": r["planner_task_id"],
            "planner_url": _planner_url(cfg, r["planner_task_id"]),
            "attachments_count": r["attachments_count"] or 0,
            "source": r["source"] or "telegram",
            "submitter_name": r["submitter_name"] or "",
            "submitter_email": r["submitter_email"] or "",
        }
        for r in result["items"]
    ]
    return _json({**result, "items": items})


def _ticket_item(cfg: Config, row: dict) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "target_department": row["target_department"] or "marketing",
        "department": row["department"],
        "request_type": row["request_type"],
        "task_type": row["task_type"],
        "summary": row["summary"],
        "deadline": row["deadline"],
        "priority": row["priority"],
        "status": row["status"] or "new",
        "status_updated_at": row["status_updated_at"] or "",
        "contact": row["contact"],
        "phone": row["phone"] or "",
        "planner_task_id": row["planner_task_id"],
        "planner_url": _planner_url(cfg, row["planner_task_id"]),
        "attachments_count": row["attachments_count"] or 0,
        "source": row["source"] or "telegram",
        "submitter_name": row["submitter_name"] or "",
        "submitter_email": row["submitter_email"] or "",
    }


async def ticket_detail(request: web.Request) -> web.Response:
    """Одна заявка по номеру + кешированный статус. С X-KNUS-Client — только свои заявки."""
    request_id = request.match_info["ticket_id"].strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{8,64}", request_id):
        return _json({"error": "invalid_ticket_id"}, status=400)
    db: RequestsDB = request.app["db"]
    row = await db.get_by_id(request_id)
    if row is None:
        return _json({"error": "not_found"}, status=404)
    client = re.sub(
        r"[^a-zA-Z0-9_.-]", "", request.headers.get("X-KNUS-Client", "").strip()
    )[:64]
    if client and row.get("source") != f"external:{client}":
        # Не раскрываем существование чужой заявки другому интеграционному клиенту.
        return _json({"error": "not_found"}, status=404)
    return _json(_ticket_item(request.app["config"], row))


# --- приём заявки с веб-портала (POST /api/tickets) -------------------------


def _resolve_choice(values: dict, key: str, custom: str, label: str) -> str:
    if key not in values:
        raise ValueError(f"Некорректное поле: {label}")
    value = values[key][1].strip()
    if key == "other":
        value = custom.strip()
    if not value:
        raise ValueError(f"Заполните поле: {label}")
    return value


def _normalize_deadline(value: str) -> str:
    value = value.strip()
    if not value:
        return NO_DEADLINE
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError("Некорректная дата дедлайна")


async def _read_submission(request: web.Request, tmp_dir: Path) -> tuple[dict[str, str], list[Path]]:
    if not request.content_type.startswith("multipart/"):
        raise ValueError("Ожидается multipart/form-data")

    cfg: Config = request.app["config"]
    fields: dict[str, str] = {}
    files: list[Path] = []
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename:
            if len(files) >= cfg.max_attachments:
                raise ValueError(f"Можно приложить не более {cfg.max_attachments} файлов")
            original_name = Path(part.filename).name or f"file-{len(files) + 1}"
            safe_name = sanitize_filename(original_name)
            local_path = tmp_dir / f"{len(files) + 1:02d}-{safe_name}"
            size = 0
            with local_path.open("wb") as fh:
                while True:
                    chunk = await part.read_chunk(size=256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_WEB_FILE_SIZE:
                        raise ValueError(f"Файл {original_name} больше 20 МБ")
                    fh.write(chunk)
            files.append(local_path)
            fields[f"_original_{len(files) - 1}"] = original_name
        else:
            fields[part.name] = (await part.text()).strip()
    return fields, files


async def _notify_web_submission(
    app: web.Application, ticket: Ticket, files: list[Path], chat_ids: tuple[str, ...]
) -> None:
    bot: Bot | None = app.get("bot")
    if not bot or not chat_ids:
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=notification_html(ticket),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            for index, local_path in enumerate(files):
                attachment = ticket.attachments[index]
                with local_path.open("rb") as fh:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=fh,
                        filename=attachment.file_name,
                        caption=f"📎 Заявка {ticket.number} — {attachment.file_name}",
                    )
        except (TelegramError, OSError) as exc:
            log.error(
                "Заявка %s: уведомление в чат %s не отправлено: %s", ticket.number, chat_id, exc
            )


async def create_ticket(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    db: RequestsDB = request.app["db"]
    planner: PlannerClient | None = request.app["planner"]
    router: TicketRouter | None = request.app.get("router")
    if planner is None or router is None:
        return _json({"error": "planner_unavailable"}, status=503)

    submitter_email = request.headers.get("X-KNUS-User-Email", "").strip().lower()
    submitter_name = unquote(request.headers.get("X-KNUS-User-Name", "").strip())
    if not submitter_email.endswith(WEB_EMAIL_DOMAIN):
        return _json({"error": "corporate_account_required"}, status=403)

    tmp_dir = Path(tempfile.mkdtemp(prefix="knus-web-"))
    # источник: web (форма портала) или external:<client>, если задан X-KNUS-Client
    source_client = re.sub(
        r"[^a-zA-Z0-9_.-]", "", request.headers.get("X-KNUS-Client", "").strip()
    )[:64]
    source = f"external:{source_client}" if source_client else "web"
    ticket = Ticket(source=source, submitter_name=submitter_name, submitter_email=submitter_email)
    try:
        try:
            fields, files = await _read_submission(request, tmp_dir)

            # отдел-исполнитель: поле target_department (по умолчанию marketing)
            target = (fields.get("target_department") or "marketing").strip()
            route = router.route(target)
            if route is None:
                raise ValueError(f"Неизвестный отдел-исполнитель: {target}")
            ticket.target_department = route.code
            ticket.target_department_name = route.name
            ticket.planner_plan_id = route.plan_id
            ticket.assignees = list(route.assignees)
            ticket.route_key = route.code

            # подразделение заявителя
            ticket.department = _resolve_choice(
                DEPARTMENTS, fields.get("department", ""),
                fields.get("department_other", ""), "подразделение",
            )

            # тип обращения — только из типов выбранного отдела
            registry: DepartmentRegistry = request.app["registry"]
            allowed = set(registry.request_types(route.code))
            task_key = fields.get("task_type", "")
            if task_key != "other" and task_key not in allowed:
                raise ValueError("Тип обращения недоступен для выбранного отдела")
            ticket.request_type = task_key
            ticket.task_type = _resolve_choice(
                REQUEST_TYPES, task_key, fields.get("task_type_other", ""), "тип обращения",
            )
            ticket.category = TASK_TYPE_CATEGORY.get(task_key, "")

            ticket.description = fields.get("description", "").strip()
            if len(ticket.description) < 10:
                raise ValueError("Описание должно содержать не менее 10 символов")
            if len(ticket.description) > 5000:
                raise ValueError("Описание не должно превышать 5000 символов")
            ticket.deadline = _normalize_deadline(fields.get("deadline", ""))
            priority_key = fields.get("priority", "normal")
            if priority_key not in PRIORITIES:
                raise ValueError("Некорректный приоритет")
            _, ticket.priority, ticket.priority_value = PRIORITIES[priority_key]
            ticket.contact = fields.get("contact", "").strip() or submitter_email
            if len(ticket.contact) > 500:
                raise ValueError("Поле контакта слишком длинное")
            # Телефон на вебе обязателен на уровне формы (required). Жёсткую серверную
            # проверку не включаем, чтобы не ломать формы без этого поля; если пришёл — сохраняем.
            ticket.phone = fields.get("phone", "").strip()[:50]
            for index, local_path in enumerate(files):
                original_name = fields.get(f"_original_{index}", local_path.name)
                ticket.attachments.append(
                    Attachment(kind="document", file_name=original_name, size=local_path.stat().st_size)
                )
        except (ValueError, web.HTTPRequestEntityTooLarge) as exc:
            return _json({"error": "validation_error", "message": str(exc)}, status=400)

        ticket.number = new_number(ticket.target_department)
        now = datetime.now()
        ticket.created_at = now.strftime("%d.%m.%Y %H:%M")
        notify_chats = route.notification_chat_ids

        # сегмент: отдельный по типу обращения (напр. EDMS), иначе общий отдела
        bkt_id, bkt_name = route.bucket_for(ticket.request_type)
        try:
            created = await planner.create_task(
                plan_id=ticket.planner_plan_id,
                bucket_id=bkt_id,
                bucket_name=bkt_name,
                title=planner_title(ticket),
                due_date_iso=ticket.deadline_iso(),
                priority=ticket.priority_value,
                applied_categories=ticket.applied_categories(),
                assignees=ticket.assignees,
            )
            ticket.planner_task_id = created["id"]
            ticket.planner_task_url = created["url"]
            await db.add(ticket, now.isoformat(timespec="seconds"))
        except Exception as exc:
            log.exception("Веб-заявка %s: не удалось создать задачу Planner", ticket.number)
            await log_request(cfg.requests_log_path, ticket, "planner_failed")
            await log_fallback(cfg.fallback_log_path, ticket, str(exc))
            await _notify_web_submission(request.app, ticket, files, notify_chats)
            return _json(
                {"error": "planner_error", "message": "Заявка не сохранена в Planner", "id": ticket.number},
                status=502,
            )

        warnings: list[str] = []
        for index, local_path in enumerate(files):
            attachment = ticket.attachments[index]
            try:
                attachment.sharepoint_url = await planner.upload_file(
                    local_path,
                    plan_id=ticket.planner_plan_id,
                    ticket_number=ticket.number,
                    file_name=attachment.file_name,
                )
            except (GraphError, OSError) as exc:
                attachment.upload_error = str(exc)
                warnings.append(f"Не удалось прикрепить {attachment.file_name} к Planner")
                log.error("Веб-заявка %s: ошибка вложения %s: %s", ticket.number, attachment.file_name, exc)

        try:
            await planner.set_details(
                ticket.planner_task_id,
                planner_description(ticket),
                [(a.sharepoint_url, a.file_name) for a in ticket.uploaded],
            )
        except GraphError as exc:
            warnings.append("Не удалось записать описание в Planner")
            log.error("Веб-заявка %s: details Planner: %s", ticket.number, exc)

        await _notify_web_submission(request.app, ticket, files, notify_chats)
        await log_request(cfg.requests_log_path, ticket, "planner_ok")
        return _json(
            {
                "ok": True,
                "id": ticket.number,
                "planner_url": ticket.planner_task_url,
                "attachments": len(ticket.attachments),
                "warnings": warnings,
            },
            status=201,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- фоновый рефрешер статусов ---------------------------------------------


def _plan_for(app: web.Application, row: dict) -> str:
    """План заявки: из строки, иначе — по отделу из реестра (старые строки без plan_id)."""
    plan = row.get("planner_plan_id")
    if plan:
        return plan
    registry = app.get("registry")
    dep = registry.get(row.get("target_department") or "marketing") if registry else None
    return dep.planner_plan_id if dep else ""


async def _refresh_once(app: web.Application) -> None:
    db: RequestsDB = app["db"]
    planner: PlannerClient | None = app["planner"]
    if planner is None:
        return

    active = await db.active_tasks()
    if not active:
        return

    # группируем активные заявки по плану и обходим каждый план ОДНИМ запросом
    by_plan: dict[str, list[dict]] = {}
    for row in active:
        plan = _plan_for(app, row)
        if plan:
            by_plan.setdefault(plan, []).append(row)

    now = datetime.now().isoformat(timespec="seconds")
    updates: list[tuple[str, str, str]] = []
    for plan_id, rows in by_plan.items():
        try:
            tasks = await planner.list_plan_tasks(plan_id)
        except GraphError as exc:
            log.warning("Рефрешер: план %s недоступен: %s", plan_id, exc)
            continue
        for row in rows:
            task = tasks.get(row["planner_task_id"])
            if task is None:
                continue  # задача удалена из плана — сохраняем последний известный статус
            updates.append((api_status(task.get("percentComplete")), now, row["id"]))

    await db.update_statuses(updates)
    if updates:
        log.info("Статусы заявок обновлены: %d (планов: %d)", len(updates), len(by_plan))


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


def build_app(
    cfg: Config,
    db: RequestsDB,
    planner: PlannerClient | None,
    registry: DepartmentRegistry | None = None,
    bot: Bot | None = None,
) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=MAX_WEB_REQUEST_SIZE)
    app["config"] = cfg
    app["db"] = db
    app["planner"] = planner
    app["registry"] = registry
    app["router"] = TicketRouter(registry) if registry is not None else None
    app["bot"] = bot
    app.router.add_get("/health", health)
    app.router.add_get("/api/registry", get_registry)  # отделы+типы (минимальный)
    app.router.add_get("/api/catalog", catalog)  # полный справочник формы
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/tickets", tickets)
    app.router.add_get("/api/tickets/{ticket_id}", ticket_detail)  # одна заявка по номеру
    app.router.add_post("/api/tickets", create_ticket)  # приём заявки с веб-портала
    return app


class ApiServer:
    """Держит AppRunner и фоновую задачу; поднимается в loop'е бота."""

    def __init__(
        self,
        cfg: Config,
        db: RequestsDB,
        planner: PlannerClient | None,
        registry: DepartmentRegistry | None = None,
        bot: Bot | None = None,
    ):
        self.cfg = cfg
        self.app = build_app(cfg, db, planner, registry, bot)
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
