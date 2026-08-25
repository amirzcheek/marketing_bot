"""Модель заявки, генерация номера и форматирование текстов."""

import random
import string
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from html import escape

from .constants import BUCKET_STATUS, CATEGORY_DEPARTMENTS, NO_DEADLINE, URGENT_CATEGORY

MAX_TITLE_GIST = 60


@dataclass
class Attachment:
    file_id: str = ""
    kind: str = ""  # photo / document / video / animation
    file_name: str = ""
    size: int = 0
    too_large: bool = False  # >20 МБ — getFile не отдаст, в SharePoint не попадёт
    sharepoint_url: str = ""
    upload_error: str = ""

    @property
    def uploaded(self) -> bool:
        return bool(self.sharepoint_url)

    @property
    def size_mb(self) -> str:
        return f"{self.size / 1024 / 1024:.1f} МБ" if self.size else "?"


@dataclass
class Ticket:
    number: str = ""
    # target_department — отдел-исполнитель (куда идёт заявка); маршрутизация по нему
    target_department: str = "marketing"  # код отдела из реестра
    target_department_name: str = ""
    planner_plan_id: str = ""  # план отдела-исполнителя (резолвится роутером)
    assignees: list[str] = field(default_factory=list)
    route_key: str = ""
    # department — подразделение ЗАЯВИТЕЛЯ (откуда подаёт); теперь просто атрибут
    department: str = ""
    request_type: str = ""  # ключ типа обращения
    task_type: str = ""  # человекочитаемое название типа
    category: str = ""  # метка Planner по типу задачи, пусто для «Другое»
    description: str = ""  # исходный текст пользователя
    description_normalized: str = ""  # результат LLM, может быть пустым
    deadline: str = ""  # "ДД.ММ.ГГГГ" или NO_DEADLINE
    priority: str = ""
    priority_value: int = 5
    contact: str = ""
    tg_username: str = ""
    tg_user_id: int = 0
    created_at: str = ""
    suggested_type: str = ""  # подсказка LLM, не навязываем
    planner_task_id: str = ""
    planner_task_url: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    # --- метки Planner -----------------------------------------------------

    def applied_categories(self) -> dict[str, bool]:
        """Метки Planner: тип задачи + пометка срочности.

        Только для отделов, у чьего плана метки настроены (сейчас маркетинг).
        У остальных планов слотов нет — вернём пусто, чтобы не вешать безымянные теги.
        """
        if self.target_department not in CATEGORY_DEPARTMENTS:
            return {}
        cats: dict[str, bool] = {}
        if self.category:
            cats[self.category] = True
        if self.priority_value == 1:
            cats[URGENT_CATEGORY] = True
        return cats

    # --- вложения ----------------------------------------------------------

    @property
    def uploadable(self) -> list[Attachment]:
        """Файлы, которые можно скачать через getFile и загрузить в SharePoint."""
        return [a for a in self.attachments if not a.too_large]

    @property
    def uploaded(self) -> list[Attachment]:
        return [a for a in self.attachments if a.uploaded]

    @property
    def too_large(self) -> list[Attachment]:
        return [a for a in self.attachments if a.too_large]

    # --- производные значения ---------------------------------------------

    @property
    def effective_description(self) -> str:
        return self.description_normalized or self.description

    @property
    def gist(self) -> str:
        """Краткая суть для заголовка задачи."""
        text = " ".join(self.effective_description.split())
        if len(text) <= MAX_TITLE_GIST:
            return text
        cut = text[:MAX_TITLE_GIST].rsplit(" ", 1)[0]
        return f"{cut or text[:MAX_TITLE_GIST]}…"

    @property
    def has_deadline(self) -> bool:
        return bool(self.deadline) and self.deadline != NO_DEADLINE

    def deadline_iso(self) -> str | None:
        """Дедлайн в формате Graph (UTC, конец рабочего дня 17:00 локально ~ просто дата)."""
        if not self.has_deadline:
            return None
        d = datetime.strptime(self.deadline, "%d.%m.%Y").replace(
            hour=12, minute=0, tzinfo=timezone.utc
        )
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def task_status(
    task: dict, bucket_names: dict[str, str], bucket_status: dict[str, str] | None = None
) -> str:
    """Статус заявки для заявителя.

    Основной источник — сегмент, по которому исполнитель двигает карточку.
    Отметка «Завершено» (percentComplete=100) перекрывает сегмент: её ставят прямо
    на карточке, не перетаскивая задачу. bucket_status — маппинг сегмент→статус отдела
    (у каждого отдела своя доска); по умолчанию общий BUCKET_STATUS.
    """
    mapping = bucket_status or BUCKET_STATUS
    if (task.get("percentComplete") or 0) >= 100:
        return "✅ Готово"

    bucket_name = bucket_names.get(task.get("bucketId") or "")
    if bucket_name:
        return mapping.get(bucket_name, bucket_name)

    return status_from_percent(task.get("percentComplete"))


def api_status(percent: int | None) -> str:
    """Статус для HTTP API / веб-панели: три состояния по percentComplete.

    Отдельно от bucket-статуса «Мои заявки» (тот детальнее, шесть состояний) —
    панели нужны ровно три плашки: new / in_progress / done.
    """
    p = percent or 0
    if p >= 100:
        return "done"
    if p > 0:
        return "in_progress"
    return "new"


def status_from_percent(percent: int | None) -> str:
    """Запасной вариант: сегмент неизвестен — судим по проценту готовности."""
    if percent is None:
        return "❔ Статус неизвестен"
    if percent >= 100:
        return "✅ Готово"
    if percent > 0:
        return "🔧 В работе"
    return "🆕 Новая"


def request_card_html(row: dict, status: str, target_name: str = "") -> str:
    """Карточка заявки из базы + актуальный статус из Planner."""
    e = escape
    lines = [
        f"<b>Заявка {e(row['id'])}</b>",
        f"<b>Статус:</b> {e(status)}",
        "",
    ]
    if target_name:
        lines.append(f"<b>Отдел:</b> {e(target_name)}")
    lines += [
        f"<b>Подразделение заявителя:</b> {e(row['department'] or '—')}",
        f"<b>Тип обращения:</b> {e(row['task_type'] or '—')}",
        f"<b>Суть:</b> {e(row['summary'] or '—')}",
        f"<b>Дедлайн:</b> {e(row['deadline'] or NO_DEADLINE)}",
        f"<b>Приоритет:</b> {e(row['priority'] or '—')}",
        f"<b>Подана:</b> {e(row['created_at'] or '—')}",
    ]
    return "\n".join(lines)


# префикс номера по отделу-исполнителю (историю MKT-… не ломаем)
NUMBER_PREFIX = {"marketing": "MKT", "it": "IT"}


def new_number(target_code: str = "marketing") -> str:
    prefix = NUMBER_PREFIX.get(target_code, (target_code[:4] or "REQ").upper())
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{date.today():%Y%m%d}-{suffix}"


def planner_title(t: Ticket) -> str:
    who = f" — {t.department}" if t.department else ""
    return f"[{t.task_type}] {t.gist}{who}"


def planner_description(t: Ticket) -> str:
    """Тело задачи в Planner (plain text — Planner не умеет разметку)."""
    lines = [
        f"Заявка № {t.number}",
        "",
        f"Отдел-исполнитель: {t.target_department_name or t.target_department}",
        f"Подразделение заявителя: {t.department or '—'}",
        f"Тип обращения: {t.task_type}",
        f"Приоритет: {t.priority}",
        f"Дедлайн: {t.deadline or NO_DEADLINE}",
        f"Контакт: {t.contact}",
        f"Telegram: {t.tg_username or '—'} (id {t.tg_user_id})",
        f"Дата подачи: {t.created_at}",
        "",
        "Описание:",
    ]
    if t.description_normalized:
        lines += [
            "Нормализовано:",
            t.description_normalized,
            "",
            "Исходный текст:",
            t.description,
        ]
    else:
        lines += ["Исходный текст:", t.description]

    if t.suggested_type and t.suggested_type != t.task_type:
        lines += ["", f"Подсказка LLM по типу задачи: {t.suggested_type}"]

    lines += ["", attachments_note(t)]

    return "\n".join(lines)


def attachments_note(t: Ticket) -> str:
    """Строка о материалах для описания задачи Planner."""
    total = len(t.attachments)
    if not total:
        return "📎 Материалов не приложено."

    uploaded = t.uploaded
    if uploaded and len(uploaded) == total:
        head = f"📎 Материалов приложено: {total} (в задаче + чат отдела)"
    elif uploaded:
        head = (
            f"📎 Материалов приложено: {total} — из них {len(uploaded)} в задаче, "
            "остальные только в чате отдела"
        )
    else:
        head = f"📎 Материалов приложено: {total} — материалы в чате отдела (SharePoint недоступен)"

    lines = [head]
    for a in t.attachments:
        if a.uploaded:
            mark = "прикреплён к задаче"
        elif a.too_large:
            mark = f"{a.size_mb}, слишком большой для загрузки — только в чате отдела"
        else:
            mark = "только в чате отдела"
        lines.append(f"  • {a.file_name} — {mark}")
    return "\n".join(lines)


def summary_html(t: Ticket) -> str:
    """Сводка для показа пользователю перед отправкой."""
    e = escape
    lines = [
        "<b>📋 Проверьте заявку</b>",
        "",
        f"<b>Кому (отдел):</b> {e(t.target_department_name or t.target_department)}",
        f"<b>Ваше подразделение:</b> {e(t.department or '—')}",
        f"<b>Тип обращения:</b> {e(t.task_type)}",
        f"<b>Описание:</b> {e(t.description)}",
    ]
    if t.description_normalized:
        lines.append(f"<b>Кратко (LLM):</b> <i>{e(t.description_normalized)}</i>")
    if t.suggested_type and t.suggested_type != t.task_type:
        lines.append(f"<i>💡 Похоже на тип «{e(t.suggested_type)}» — можно исправить.</i>")
    lines.append(f"<b>Материалы:</b> {e(_attachments_short(t))}")
    lines += [
        f"<b>Дедлайн:</b> {e(t.deadline or NO_DEADLINE)}",
        f"<b>Приоритет:</b> {e(t.priority)}",
        f"<b>Контакт:</b> {e(t.contact)}",
    ]
    return "\n".join(lines)


def _attachments_short(t: Ticket) -> str:
    if not t.attachments:
        return "нет"
    names = ", ".join(a.file_name for a in t.attachments[:3])
    if len(t.attachments) > 3:
        names += f" и ещё {len(t.attachments) - 3}"
    return f"{len(t.attachments)} шт. — {names}"


def notification_html(t: Ticket) -> str:
    """Карточка новой заявки для чата маркетинга."""
    e = escape
    head = f"🆕 <b>Новая заявка {e(t.number)}</b>"
    if t.planner_task_url:
        head += f'\n🔗 <a href="{e(t.planner_task_url)}">Открыть в Planner</a>'
    else:
        head += "\n⚠️ <i>В Planner не сохранена (см. fallback-лог)</i>"

    lines = [
        head,
        "",
        f"<b>Отдел:</b> {e(t.target_department_name or t.target_department)}",
        f"<b>Подразделение заявителя:</b> {e(t.department or '—')}",
        f"<b>Тип:</b> {e(t.task_type)}",
        f"<b>Приоритет:</b> {e(t.priority)}",
        f"<b>Дедлайн:</b> {e(t.deadline or NO_DEADLINE)}",
        "",
        f"<b>Суть:</b> {e(t.effective_description)}",
        "",
        f"<b>Контакт:</b> {e(t.contact)}",
        f"<b>Telegram:</b> {e(t.tg_username or '—')}",
    ]
    if t.attachments:
        lines += ["", f"📎 <b>Материалов:</b> {len(t.attachments)} — файлы ниже ⬇️"]
    return "\n".join(lines)
