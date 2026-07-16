"""Точка входа: Telegram-бот «Заявки в отдел маркетинга» КНУС. Long polling."""

import logging
import sys

from telegram import BotCommand
from telegram.ext import Application, Defaults

from bot.api import ApiServer
from bot.config import load_config
from bot.db import RequestsDB
from bot.graph import PlannerClient
from bot.handlers import register
from bot.llm import LLMClient

log = logging.getLogger("marketing-bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


async def _post_init(app: Application) -> None:
    await app.bot_data["db"].init()

    # HTTP API поднимаем в этом же loop'е, параллельно polling'у
    api = ApiServer(app.bot_data["config"], app.bot_data["db"], app.bot_data["planner"])
    await api.start()
    app.bot_data["api"] = api

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Подать заявку в отдел маркетинга"),
            BotCommand("my", "Мои заявки и их статус"),
            BotCommand("cancel", "Сбросить текущий диалог"),
            BotCommand("myid", "Показать chat_id этого чата"),
            BotCommand("help", "Справка"),
        ]
    )
    me = await app.bot.get_me()
    log.info("Бот запущен: @%s (id %s)", me.username, me.id)


async def _post_shutdown(app: Application) -> None:
    api: ApiServer | None = app.bot_data.get("api")
    llm: LLMClient = app.bot_data.get("llm")
    planner: PlannerClient | None = app.bot_data.get("planner")
    if api:
        await api.stop()
    if llm:
        await llm.aclose()
    if planner:
        await planner.aclose()


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("Конфигурация: %s", p)
        sys.exit(1)

    app = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .defaults(Defaults(block=False))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.bot_data["config"] = cfg
    app.bot_data["llm"] = LLMClient(cfg)
    app.bot_data["planner"] = PlannerClient(cfg) if cfg.planner_enabled else None
    app.bot_data["db"] = RequestsDB(cfg.requests_db_path)

    if not cfg.planner_enabled:
        log.warning("PLANNER_ENABLED=false — заявки только логируются в %s", cfg.requests_log_path)
    if not cfg.llm_normalize:
        log.info("LLM_NORMALIZE=false — нормализация описаний отключена")
    if not cfg.marketing_chat_id:
        log.warning("MARKETING_CHAT_ID не задан — уведомления маркетингу отправляться не будут")

    register(app)

    log.info("Запуск long polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
