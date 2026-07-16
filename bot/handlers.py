"""Машина состояний диалога заявки."""

import logging
import shutil
import tempfile
from datetime import date, datetime
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import Config
from .constants import (
    DEPARTMENTS,
    NO_DEADLINE,
    PRIORITIES,
    TASK_TYPE_CATEGORY,
    TASK_TYPES,
    TELEGRAM_DOWNLOAD_LIMIT,
)
from .db import RequestsDB
from .graph import GraphError, PlannerClient, sanitize_filename
from .llm import LLMClient
from .storage import log_fallback, log_request
from .ticket import (
    Attachment,
    Ticket,
    new_number,
    notification_html,
    planner_description,
    planner_title,
    request_card_html,
    status_from_percent,
    summary_html,
)

log = logging.getLogger(__name__)

(
    DEPARTMENT,
    DEPARTMENT_OTHER,
    TASK_TYPE,
    TASK_TYPE_OTHER,
    DESCRIPTION,
    ATTACH_ASK,
    ATTACH_COLLECT,
    DEADLINE,
    PRIORITY,
    CONTACT,
    CONFIRM,
    EDIT_MENU,
) = range(12)

MIN_DESCRIPTION_LEN = 10


# --- клавиатуры -------------------------------------------------------------


def _departments_kb(include_my: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"dep:{key}")]
        for key, (label, _) in DEPARTMENTS.items()
    ]
    if include_my:
        rows.append([InlineKeyboardButton("📋 Мои заявки", callback_data="my:list:0")])
    return InlineKeyboardMarkup(rows)


def _task_types_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"type:{key}")]
        for key, (label, _) in TASK_TYPES.items()
    ]
    return InlineKeyboardMarkup(rows)


def _priority_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"prio:{key}")] for key, (label, _, _) in PRIORITIES.items()]
    )


def _deadline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗓 Без срока", callback_data="deadline:none")]])


def _attach_ask_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📎 Приложить", callback_data="attach:yes")],
            [InlineKeyboardButton("Пропустить", callback_data="attach:skip")],
        ]
    )


def _attach_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data="attach:done")]])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Отправить", callback_data="final:send")],
            [InlineKeyboardButton("✏️ Исправить", callback_data="final:edit")],
            [InlineKeyboardButton("❌ Отмена", callback_data="final:cancel")],
        ]
    )


def _edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Департамент", callback_data="edit:department")],
            [InlineKeyboardButton("Тип задачи", callback_data="edit:type")],
            [InlineKeyboardButton("Описание", callback_data="edit:description")],
            [InlineKeyboardButton("Материалы", callback_data="edit:attachments")],
            [InlineKeyboardButton("Дедлайн", callback_data="edit:deadline")],
            [InlineKeyboardButton("Приоритет", callback_data="edit:priority")],
            [InlineKeyboardButton("Контакт", callback_data="edit:contact")],
            [InlineKeyboardButton("⬅️ Назад к сводке", callback_data="edit:back")],
        ]
    )


# --- утилиты ----------------------------------------------------------------


def _ticket(context: ContextTypes.DEFAULT_TYPE) -> Ticket:
    ticket = context.user_data.get("ticket")
    if ticket is None:
        ticket = Ticket()
        context.user_data["ticket"] = ticket
    return ticket


def _editing(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("editing"))


async def _show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["editing"] = False
    ticket = _ticket(context)
    text = summary_html(ticket)
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=_confirm_kb(), parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=_confirm_kb(), parse_mode=ParseMode.HTML
        )
    return CONFIRM


# --- шаги диалога -----------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["ticket"] = Ticket()
    user = update.effective_user
    await update.effective_message.reply_text(
        f"👋 Здравствуйте, {escape(user.first_name or 'коллега')}!\n\n"
        "Это бот приёма заявок в <b>отдел маркетинга КНУС</b>.\n"
        "Заполним заявку за 7 шагов — займёт пару минут.\n\n"
        "<b>Шаг 1/7.</b> Выберите ваш департамент:",
        reply_markup=_departments_kb(include_my=True),
        parse_mode=ParseMode.HTML,
    )
    return DEPARTMENT


async def department_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    label, full_name = DEPARTMENTS[key]

    if key == "other":
        await query.edit_message_text("✏️ Введите название вашего департамента текстом:")
        return DEPARTMENT_OTHER

    ticket = _ticket(context)
    ticket.department = full_name
    await query.edit_message_text(f"Департамент: <b>{escape(full_name)}</b>", parse_mode=ParseMode.HTML)

    if _editing(context):
        return await _show_summary(update, context)

    await query.message.reply_text(
        "<b>Шаг 2/7.</b> Какой тип задачи?", reply_markup=_task_types_kb(), parse_mode=ParseMode.HTML
    )
    return TASK_TYPE


async def department_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Название не может быть пустым. Введите ещё раз:")
        return DEPARTMENT_OTHER

    ticket = _ticket(context)
    ticket.department = text[:200]

    if _editing(context):
        return await _show_summary(update, context)

    await update.effective_message.reply_text(
        "<b>Шаг 2/7.</b> Какой тип задачи?", reply_markup=_task_types_kb(), parse_mode=ParseMode.HTML
    )
    return TASK_TYPE


async def task_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    _, name = TASK_TYPES[key]

    if key == "other":
        await query.edit_message_text("✏️ Опишите тип задачи одним-двумя словами:")
        return TASK_TYPE_OTHER

    ticket = _ticket(context)
    ticket.task_type = name
    ticket.category = TASK_TYPE_CATEGORY.get(key, "")
    await query.edit_message_text(f"Тип задачи: <b>{escape(name)}</b>", parse_mode=ParseMode.HTML)

    if _editing(context):
        return await _show_summary(update, context)

    await query.message.reply_text(
        "<b>Шаг 3/7.</b> Опишите задачу: что нужно сделать, для чего, пожелания по формату.",
        parse_mode=ParseMode.HTML,
    )
    return DESCRIPTION


async def task_type_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Тип не может быть пустым. Введите ещё раз:")
        return TASK_TYPE_OTHER

    ticket = _ticket(context)
    ticket.task_type = text[:100]
    ticket.category = ""  # произвольный тип метке не соответствует

    if _editing(context):
        return await _show_summary(update, context)

    await update.effective_message.reply_text(
        "<b>Шаг 3/7.</b> Опишите задачу: что нужно сделать, для чего, пожелания по формату.",
        parse_mode=ParseMode.HTML,
    )
    return DESCRIPTION


async def description_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if len(text) < MIN_DESCRIPTION_LEN:
        await update.effective_message.reply_text(
            "Слишком коротко — опишите задачу подробнее (что нужно, для чего, формат):"
        )
        return DESCRIPTION

    ticket = _ticket(context)
    ticket.description = text[:4000]
    ticket.description_normalized = ""
    ticket.suggested_type = ""

    llm: LLMClient = context.bot_data["llm"]
    if llm.enabled:
        notice = await update.effective_message.reply_text("⏳ Обрабатываю описание…")
        normalized = await llm.normalize(ticket.description)
        if normalized:
            ticket.description_normalized = normalized
        suggested = await llm.suggest_type(ticket.description)
        if suggested:
            ticket.suggested_type = suggested
        try:
            await notice.delete()
        except TelegramError:
            pass

        if ticket.description_normalized:
            await update.effective_message.reply_text(
                f"📝 Кратко: <i>{escape(ticket.description_normalized)}</i>",
                parse_mode=ParseMode.HTML,
            )
        if ticket.suggested_type and ticket.suggested_type != ticket.task_type:
            await update.effective_message.reply_text(
                f"💡 Похоже, это тип «<b>{escape(ticket.suggested_type)}</b>». "
                "Если согласны — сможете исправить на шаге сводки.",
                parse_mode=ParseMode.HTML,
            )

    if _editing(context):
        return await _show_summary(update, context)

    await update.effective_message.reply_text(
        "<b>Шаг 4/7.</b> Приложить материалы? (фото, видео, документы — примеры дизайна, референсы)",
        reply_markup=_attach_ask_kb(),
        parse_mode=ParseMode.HTML,
    )
    return ATTACH_ASK


# --- вложения ---------------------------------------------------------------


async def attach_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    cfg: Config = context.bot_data["config"]

    if action == "skip":
        await query.edit_message_text("Без материалов.")
        return await _after_attachments(update, context)

    await query.edit_message_text(
        f"📎 Пришлите файлы — фото, видео, документы (до {cfg.max_attachments} шт.).\n"
        "Можно несколько подряд. Когда закончите — нажмите «Готово».",
        reply_markup=_attach_done_kb(),
    )
    return ATTACH_COLLECT


def _extract_attachment(message, index: int) -> Attachment | None:
    """Достаёт file_id/имя/размер из сообщения. Скачивания здесь нет."""
    if message.photo:
        photo = message.photo[-1]  # последний размер — самый крупный
        return Attachment(
            file_id=photo.file_id, kind="photo", file_name=f"photo_{index}.jpg", size=photo.file_size or 0
        )
    if message.document:
        doc = message.document
        return Attachment(
            file_id=doc.file_id,
            kind="document",
            file_name=doc.file_name or f"document_{index}",
            size=doc.file_size or 0,
        )
    if message.video:
        video = message.video
        return Attachment(
            file_id=video.file_id,
            kind="video",
            file_name=video.file_name or f"video_{index}.mp4",
            size=video.file_size or 0,
        )
    if message.animation:
        anim = message.animation
        return Attachment(
            file_id=anim.file_id,
            kind="animation",
            file_name=anim.file_name or f"animation_{index}.mp4",
            size=anim.file_size or 0,
        )
    return None


async def attach_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["config"]
    ticket = _ticket(context)
    message = update.effective_message

    if len(ticket.attachments) >= cfg.max_attachments:
        await message.reply_text(
            f"Достигнут лимит в {cfg.max_attachments} файлов. Нажмите «Готово» или уберите лишнее "
            "и подайте заявку заново.",
            reply_markup=_attach_done_kb(),
        )
        return ATTACH_COLLECT

    attachment = _extract_attachment(message, len(ticket.attachments) + 1)
    if attachment is None:
        await message.reply_text(
            "Это не файл. Пришлите фото, видео или документ — либо нажмите «Готово».",
            reply_markup=_attach_done_kb(),
        )
        return ATTACH_COLLECT

    # Файл больше лимита getFile: переслать в чат по file_id сможем, скачать — нет.
    attachment.too_large = attachment.size > TELEGRAM_DOWNLOAD_LIMIT
    ticket.attachments.append(attachment)

    text = f"✅ Принято: <b>{escape(attachment.file_name)}</b> ({len(ticket.attachments)}/{cfg.max_attachments})"
    if attachment.too_large:
        text += (
            f"\n⚠️ Файл большой ({attachment.size_mb}) — он уйдёт в чат маркетинга, "
            "но не сохранится в общей папке. Если он важен, пришлите ссылкой в описании."
        )
    await message.reply_text(text, reply_markup=_attach_done_kb(), parse_mode=ParseMode.HTML)
    return ATTACH_COLLECT


async def attach_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket = _ticket(context)
    count = len(ticket.attachments)
    await query.edit_message_text(
        f"📎 Материалов приложено: {count}" if count else "Без материалов."
    )
    return await _after_attachments(update, context)


async def _after_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if _editing(context):
        return await _show_summary(update, context)

    message = update.callback_query.message if update.callback_query else update.effective_message
    await message.reply_text(
        "<b>Шаг 5/7.</b> К какому сроку нужно? Введите дату в формате <code>ДД.ММ.ГГГГ</code>.",
        reply_markup=_deadline_kb(),
        parse_mode=ParseMode.HTML,
    )
    return DEADLINE


async def deadline_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    try:
        parsed = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Не похоже на дату. Нужен формат <code>ДД.ММ.ГГГГ</code>, например <code>25.08.2026</code>.\n"
            "Или нажмите «Без срока».",
            reply_markup=_deadline_kb(),
            parse_mode=ParseMode.HTML,
        )
        return DEADLINE

    if parsed < date.today():
        await update.effective_message.reply_text(
            "❌ Эта дата уже прошла. Введите дату не раньше сегодняшней:",
            reply_markup=_deadline_kb(),
        )
        return DEADLINE

    ticket = _ticket(context)
    ticket.deadline = parsed.strftime("%d.%m.%Y")

    if _editing(context):
        return await _show_summary(update, context)

    await update.effective_message.reply_text(
        "<b>Шаг 6/7.</b> Насколько срочно?", reply_markup=_priority_kb(), parse_mode=ParseMode.HTML
    )
    return PRIORITY


async def deadline_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket = _ticket(context)
    ticket.deadline = NO_DEADLINE
    await query.edit_message_text("Дедлайн: <b>без срока</b>", parse_mode=ParseMode.HTML)

    if _editing(context):
        return await _show_summary(update, context)

    await query.message.reply_text(
        "<b>Шаг 6/7.</b> Насколько срочно?", reply_markup=_priority_kb(), parse_mode=ParseMode.HTML
    )
    return PRIORITY


async def priority_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    _, name, value = PRIORITIES[key]

    ticket = _ticket(context)
    ticket.priority = name
    ticket.priority_value = value
    await query.edit_message_text(f"Приоритет: <b>{escape(name)}</b>", parse_mode=ParseMode.HTML)

    if _editing(context):
        return await _show_summary(update, context)

    await query.message.reply_text(
        "<b>Шаг 7/7.</b> Контакт для уточнений — имя и телефон или почта.\n"
        "Например: <code>Айгуль Смагулова, +7 701 123 45 67</code>",
        parse_mode=ParseMode.HTML,
    )
    return CONTACT


async def contact_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if len(text) < 3:
        await update.effective_message.reply_text(
            "Контакт не может быть пустым. Укажите имя и телефон или почту:"
        )
        return CONTACT

    ticket = _ticket(context)
    ticket.contact = text[:300]
    return await _show_summary(update, context)


# --- сводка / правка / отправка --------------------------------------------


async def final_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        await query.edit_message_text("❌ Заявка отменена. Нажмите /start, чтобы начать заново.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "edit":
        await query.edit_message_text("Что исправить?", reply_markup=_edit_kb())
        return EDIT_MENU

    return await _submit(update, context)


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]

    if field == "back":
        return await _show_summary(update, context)

    context.user_data["editing"] = True

    if field == "department":
        await query.edit_message_text("Выберите департамент:", reply_markup=_departments_kb())
        return DEPARTMENT
    if field == "type":
        await query.edit_message_text("Выберите тип задачи:", reply_markup=_task_types_kb())
        return TASK_TYPE
    if field == "description":
        await query.edit_message_text("Введите новое описание задачи:")
        return DESCRIPTION
    if field == "attachments":
        cfg: Config = context.bot_data["config"]
        ticket = _ticket(context)
        ticket.attachments.clear()  # проще пересобрать список, чем удалять по одному
        await query.edit_message_text(
            f"Прежние материалы убраны. Пришлите файлы заново (до {cfg.max_attachments} шт.) "
            "и нажмите «Готово».\nЕсли материалы не нужны — сразу «Готово».",
            reply_markup=_attach_done_kb(),
        )
        return ATTACH_COLLECT
    if field == "deadline":
        await query.edit_message_text(
            "Введите дату в формате ДД.ММ.ГГГГ или нажмите «Без срока»:",
            reply_markup=_deadline_kb(),
        )
        return DEADLINE
    if field == "priority":
        await query.edit_message_text("Выберите приоритет:", reply_markup=_priority_kb())
        return PRIORITY
    if field == "contact":
        await query.edit_message_text("Введите контакт для уточнений:")
        return CONTACT

    return CONFIRM


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    cfg: Config = context.bot_data["config"]
    planner: PlannerClient | None = context.bot_data.get("planner")

    ticket = _ticket(context)
    ticket.number = new_number()
    now = datetime.now()
    ticket.created_at = now.strftime("%d.%m.%Y %H:%M")
    user = update.effective_user
    ticket.tg_username = f"@{user.username}" if user.username else (user.full_name or "")
    ticket.tg_user_id = user.id

    await query.edit_message_text("⏳ Отправляю заявку…")

    error: str | None = None
    if planner is None:
        error = "Planner отключён (PLANNER_ENABLED=false)"
        log.info("Заявка %s: Planner отключён, только локальный лог", ticket.number)
    else:
        try:
            created = await planner.create_task(
                title=planner_title(ticket),
                due_date_iso=ticket.deadline_iso(),
                priority=ticket.priority_value,
                applied_categories=ticket.applied_categories(),
            )
            ticket.planner_task_id = created["id"]
            ticket.planner_task_url = created["url"]
            # связь юзер→заявка нужна для «Мои заявки»; пишем только при успехе в Planner,
            # иначе статус читать будет неоткуда
            db: RequestsDB = context.bot_data["db"]
            await db.add(ticket, now.isoformat(timespec="seconds"))
        except GraphError as exc:
            error = str(exc)
        except Exception as exc:  # неожиданная ошибка не должна ронять диалог
            log.exception("Неожиданная ошибка при создании задачи в Planner")
            error = repr(exc)

    # Отвечаем заявителю сразу — загрузка файлов в SharePoint его ждать не должна.
    if error and planner is not None:
        await query.message.reply_text(
            f"⚠️ Заявка <b>{escape(ticket.number)}</b> временно не сохранена — "
            "попробуйте позже или свяжитесь с отделом маркетинга напрямую.\n"
            "Мы записали её у себя, она не потеряется.",
            parse_mode=ParseMode.HTML,
        )
    else:
        text = (
            f"✅ Заявка <b>{escape(ticket.number)}</b> принята!\n\n"
            f"Отдел маркетинга получил уведомление и свяжется по контакту:\n"
            f"{escape(ticket.contact)}\n\n"
            f"Номер заявки пригодится, если будете уточнять статус."
        )
        if ticket.planner_task_url:
            text += (
                f'\n\n🔗 <a href="{escape(ticket.planner_task_url)}">'
                "Задача в Planner — отслеживать статус</a>"
            )
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)

    # Быстрый путь: карточка и файлы по file_id — байты через бота не идут.
    await _notify_marketing(context, ticket)
    await _forward_attachments(context, ticket)

    # Медленный путь: каждый файл скачивается РОВНО ОДИН раз — ради SharePoint.
    if ticket.planner_task_id and ticket.uploadable:
        await _upload_attachments(context, ticket)

    # details пишем один раз — когда уже известен результат загрузки файлов.
    if ticket.planner_task_id:
        try:
            await planner.set_details(
                ticket.planner_task_id,
                planner_description(ticket),
                [(a.sharepoint_url, a.file_name) for a in ticket.uploaded],
            )
        except GraphError as exc:
            log.error(
                "Задача %s создана, но описание/вложения не записаны: %s", ticket.planner_task_id, exc
            )

    status = "planner_ok" if ticket.planner_task_id else ("local_only" if planner is None else "planner_failed")
    await log_request(cfg.requests_log_path, ticket, status)
    if error and planner is not None:
        await log_fallback(cfg.fallback_log_path, ticket, error)

    await query.message.reply_text("Нужна ещё одна заявка? Нажмите /start.")
    # Пока грузились файлы, пользователь мог начать новую заявку — чужой ticket не трогаем.
    if context.user_data.get("ticket") is ticket:
        context.user_data.clear()
    return ConversationHandler.END


async def _forward_attachments(context: ContextTypes.DEFAULT_TYPE, ticket: Ticket) -> None:
    """Пересылает файлы в чат маркетинга по file_id — без скачивания, мгновенно."""
    cfg: Config = context.bot_data["config"]
    if not cfg.marketing_chat_id or not ticket.attachments:
        return

    senders = {
        "photo": context.bot.send_photo,
        "document": context.bot.send_document,
        "video": context.bot.send_video,
        "animation": context.bot.send_animation,
    }
    for attachment in ticket.attachments:
        send = senders.get(attachment.kind, context.bot.send_document)
        caption = f"📎 Заявка {ticket.number} — {attachment.file_name}"
        try:
            await send(chat_id=cfg.marketing_chat_id, **{attachment.kind: attachment.file_id}, caption=caption)
        except TelegramError as exc:
            log.error(
                "Заявка %s: не удалось переслать %s в чат маркетинга: %s",
                ticket.number,
                attachment.file_name,
                exc,
            )


async def _upload_attachments(context: ContextTypes.DEFAULT_TYPE, ticket: Ticket) -> None:
    """Скачивает файлы из Telegram (по разу) и грузит в библиотеку группы."""
    planner: PlannerClient = context.bot_data["planner"]
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{ticket.number}_"))
    try:
        for attachment in ticket.uploadable:
            local_path = tmp_dir / sanitize_filename(attachment.file_name)
            try:
                tg_file = await context.bot.get_file(attachment.file_id)
                await tg_file.download_to_drive(local_path)
                attachment.sharepoint_url = await planner.upload_file(
                    local_path, ticket_number=ticket.number, file_name=attachment.file_name
                )
                log.info("Заявка %s: %s загружен в SharePoint", ticket.number, attachment.file_name)
            except (GraphError, TelegramError, OSError) as exc:
                # Файл уже в чате маркетинга — заявку из-за этого не роняем.
                attachment.upload_error = str(exc)
                log.error(
                    "Заявка %s: не удалось загрузить %s в SharePoint: %s",
                    ticket.number,
                    attachment.file_name,
                    exc,
                )
            finally:
                local_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _notify_marketing(context: ContextTypes.DEFAULT_TYPE, ticket: Ticket) -> None:
    cfg: Config = context.bot_data["config"]
    if not cfg.marketing_chat_id:
        log.warning("MARKETING_CHAT_ID не задан — уведомление о заявке %s не отправлено", ticket.number)
        return
    try:
        await context.bot.send_message(
            chat_id=cfg.marketing_chat_id,
            text=notification_html(ticket),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        log.error(
            "Не удалось отправить уведомление в MARKETING_CHAT_ID=%s: %s. "
            "Убедись, что чат существует и пользователь/группа начали диалог с ботом.",
            cfg.marketing_chat_id,
            exc,
        )


# --- мои заявки -------------------------------------------------------------

PAGE_SIZE = 8
STATUS_UNAVAILABLE = "❔ Статус недоступен — уточните у отдела маркетинга"


def _short(text: str, limit: int = 30) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _my_list_kb(rows: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = rows[start : start + PAGE_SIZE]

    keyboard = [
        [
            InlineKeyboardButton(
                f"№{r['id']} · {r['task_type']} · {_short(r['summary'])}",
                callback_data=f"my:open:{r['id']}",
            )
        ]
        for r in chunk
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"my:list:{page - 1}"))
    if start + PAGE_SIZE < len(rows):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"my:list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    return InlineKeyboardMarkup(keyboard)


async def _render_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    db: RequestsDB = context.bot_data["db"]
    rows = await db.list_for_user(update.effective_user.id)
    query = update.callback_query

    if not rows:
        text = (
            "У вас пока нет заявок.\n\n"
            "Нажмите /start, чтобы подать первую."
        )
        if query:
            await query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return

    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    header = f"📋 <b>Ваши заявки: {len(rows)}</b>"
    if pages > 1:
        header += f"\nСтраница {page + 1} из {pages}"
    header += "\n\nВыберите заявку, чтобы посмотреть статус:"

    if query:
        await query.edit_message_text(
            header, reply_markup=_my_list_kb(rows, page), parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            header, reply_markup=_my_list_kb(rows, page), parse_mode=ParseMode.HTML
        )


async def _resolve_status(context: ContextTypes.DEFAULT_TYPE, row: dict) -> str:
    """Статус читаем из Planner в момент просмотра — маркетолог мог его поменять."""
    planner: PlannerClient | None = context.bot_data.get("planner")
    if planner is None or not row.get("planner_task_id"):
        return STATUS_UNAVAILABLE
    try:
        task = await planner.get_task(row["planner_task_id"])
    except GraphError:
        return STATUS_UNAVAILABLE
    except Exception:
        log.exception("Неожиданная ошибка при чтении статуса задачи %s", row["planner_task_id"])
        return STATUS_UNAVAILABLE

    if task is None:  # задачу удалили из плана
        return STATUS_UNAVAILABLE
    return status_from_percent(task.get("percentComplete"))


async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_list(update, context, 0)


async def my_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, action, value = query.data.split(":", 2)

    if action == "list":
        await _render_list(update, context, int(value))
        return

    db: RequestsDB = context.bot_data["db"]
    row = await db.get(value, update.effective_user.id)
    if row is None:
        await query.edit_message_text("Заявка не найдена. Нажмите /my, чтобы обновить список.")
        return

    status = await _resolve_status(context, row)
    await query.edit_message_text(
        request_card_html(row, status),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ К списку", callback_data="my:list:0")]]
        ),
        parse_mode=ParseMode.HTML,
    )


# --- служебные команды ------------------------------------------------------


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "❌ Диалог сброшен. Нажмите /start, чтобы подать заявку заново."
    )
    return ConversationHandler.END


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"chat_id: <code>{chat.id}</code>\nтип чата: {chat.type}\n\n"
        "Это значение можно указать в MARKETING_CHAT_ID.",
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Бот приёма заявок в отдел маркетинга КНУС.\n\n"
        "/start — подать заявку\n"
        "/my — мои заявки и их статус\n"
        "/cancel — сбросить текущий диалог\n"
        "/myid — показать chat_id этого чата"
    )


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Не понял. Нажмите /start, чтобы подать заявку."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка при обработке апдейта", exc_info=context.error)


def build_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DEPARTMENT: [CallbackQueryHandler(department_chosen, pattern=r"^dep:")],
            DEPARTMENT_OTHER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, department_other)
            ],
            TASK_TYPE: [CallbackQueryHandler(task_type_chosen, pattern=r"^type:")],
            TASK_TYPE_OTHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_type_other)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_entered)],
            ATTACH_ASK: [CallbackQueryHandler(attach_ask, pattern=r"^attach:(yes|skip)$")],
            ATTACH_COLLECT: [
                CallbackQueryHandler(attach_done, pattern=r"^attach:done$"),
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.ANIMATION,
                    attach_collect,
                ),
                # текст на этом шаге — подскажем, что ждём файл или «Готово»
                MessageHandler(filters.TEXT & ~filters.COMMAND, attach_collect),
            ],
            DEADLINE: [
                CallbackQueryHandler(deadline_skipped, pattern=r"^deadline:none$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_entered),
            ],
            PRIORITY: [CallbackQueryHandler(priority_chosen, pattern=r"^prio:")],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_entered)],
            CONFIRM: [CallbackQueryHandler(final_action, pattern=r"^final:")],
            EDIT_MENU: [CallbackQueryHandler(edit_choice, pattern=r"^edit:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )


def register(app: Application) -> None:
    app.add_handler(build_conversation())
    # «Мои заявки» живёт вне диалога: ConversationHandler эти апдейты не разбирает,
    # поэтому они долетают сюда даже посреди заполнения заявки
    app.add_handler(CommandHandler("my", my_command))
    app.add_handler(CallbackQueryHandler(my_callback, pattern=r"^my:"))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))
    app.add_error_handler(on_error)
