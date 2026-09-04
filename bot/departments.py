"""Реестр отделов-исполнителей: чтение departments.yaml, валидация, доступ по коду.

Отдел-исполнитель (target_department) определяет, в какой план Planner создавать задачу,
в какой чат слать уведомление и кого назначать. Список типов обращений — тоже свой у отдела.

Обратная совместимость: если файл отсутствует/пуст/битый — синтезируем единственный отдел
«marketing» из .env, чтобы текущий прод не сломался. Пустые поля отдела marketing
подхватываются из .env.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Department:
    code: str
    name: str
    enabled: bool
    planner_plan_id: str
    planner_bucket_id: str
    planner_bucket_name: str
    notification_chat_ids: tuple[str, ...]  # уведомления могут идти в несколько чатов
    assignees: tuple[str, ...]
    request_types: tuple[str, ...]
    bucket_status: dict[str, str] = field(default_factory=dict)
    # тип обращения → id сегмента: заявки этого типа падают в отдельную колонку
    type_buckets: dict[str, str] = field(default_factory=dict)


class DepartmentRegistry:
    def __init__(self, departments: dict[str, Department]):
        self._departments = departments

    # --- доступ -------------------------------------------------------------

    def get(self, code: str) -> Department | None:
        return self._departments.get(code)

    def enabled(self) -> list[Department]:
        return [d for d in self._departments.values() if d.enabled]

    def request_types(self, code: str) -> tuple[str, ...]:
        dep = self._departments.get(code)
        return dep.request_types if dep else ()

    @property
    def single_enabled(self) -> Department | None:
        """Единственный включённый отдел (тогда шаг выбора получателя можно пропустить)."""
        enabled = self.enabled()
        return enabled[0] if len(enabled) == 1 else None


def _marketing_from_env(cfg: Config) -> Department:
    """Отдел маркетинга целиком из .env — резервный вариант без реестра."""
    return Department(
        code="marketing",
        name="Отдел маркетинга",
        enabled=True,
        planner_plan_id=cfg.planner_plan_id,
        planner_bucket_id=cfg.planner_bucket_id,
        planner_bucket_name=cfg.planner_bucket_name,
        notification_chat_ids=_parse_chat_ids(cfg.marketing_chat_id),
        assignees=(),
        request_types=("design", "video", "social_media", "website", "polygraphy", "event", "other"),
        bucket_status={},
    )


def _parse_chat_ids(value) -> tuple[str, ...]:
    """Один chat_id, список или строка через запятую/пробел → кортеж chat_id."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    return tuple(dict.fromkeys(str(v).strip() for v in items if str(v).strip()))


def _parse_department(code: str, raw: dict, cfg: Config) -> Department:
    def _s(key: str, default: str = "") -> str:
        return str(raw.get(key) or default).strip()

    plan_id = _s("planner_plan_id")
    bucket_id = _s("planner_bucket_id")
    bucket_name = _s("planner_bucket_name")

    # notification_chat_id: строка/список/через запятую. Пусто → из .env <КОД_ОТДЕЛА>_CHAT_ID
    # (marketing → MARKETING_CHAT_ID, it → IT_CHAT_ID), там тоже можно несколько через запятую.
    chat_ids = _parse_chat_ids(raw.get("notification_chat_id"))
    if not chat_ids:
        chat_ids = _parse_chat_ids(os.getenv(f"{code.upper()}_CHAT_ID"))

    # У маркетинга ещё и план/сегмент подхватываются из .env — обратная совместимость.
    if code == "marketing":
        plan_id = plan_id or cfg.planner_plan_id
        bucket_id = bucket_id or cfg.planner_bucket_id
        bucket_name = bucket_name or cfg.planner_bucket_name

    assignees = tuple(str(a).strip() for a in (raw.get("assignees") or []) if str(a).strip())
    request_types = tuple(str(t).strip() for t in (raw.get("request_types") or []) if str(t).strip())
    bucket_status = {
        str(k): str(v) for k, v in (raw.get("bucket_status") or {}).items() if k and v
    }
    type_buckets = {
        str(k): str(v) for k, v in (raw.get("type_buckets") or {}).items() if k and v
    }

    return Department(
        code=code,
        name=_s("name", code),
        enabled=bool(raw.get("enabled", True)),
        planner_plan_id=plan_id,
        planner_bucket_id=bucket_id,
        planner_bucket_name=bucket_name,
        notification_chat_ids=chat_ids,
        assignees=assignees,
        request_types=request_types,
        bucket_status=bucket_status,
        type_buckets=type_buckets,
    )


def load_registry(cfg: Config) -> DepartmentRegistry:
    path = Path(cfg.departments_config)
    if not path.exists():
        log.warning(
            "Реестр отделов %s не найден — работаю в режиме одного отдела (маркетинг из .env)",
            path,
        )
        return DepartmentRegistry({"marketing": _marketing_from_env(cfg)})

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error(
            "Не удалось прочитать реестр отделов %s (%s) — fallback на маркетинг из .env", path, exc
        )
        return DepartmentRegistry({"marketing": _marketing_from_env(cfg)})

    raw_departments = data.get("departments") or {}
    departments: dict[str, Department] = {}
    for code, raw in raw_departments.items():
        if not isinstance(raw, dict):
            continue
        dep = _parse_department(str(code), raw, cfg)
        # валидация: у включённого отдела обязателен план
        if dep.enabled and not dep.planner_plan_id:
            log.error(
                "Отдел %r включён, но без planner_plan_id — отключаю его", code
            )
            dep = Department(**{**dep.__dict__, "enabled": False})
        if dep.enabled and not dep.request_types:
            log.error("Отдел %r включён, но без request_types — отключаю его", code)
            dep = Department(**{**dep.__dict__, "enabled": False})
        departments[dep.code] = dep

    if not any(d.enabled for d in departments.values()):
        log.error("В реестре нет ни одного включённого отдела — fallback на маркетинг из .env")
        return DepartmentRegistry({"marketing": _marketing_from_env(cfg)})

    log.info(
        "Реестр отделов загружен: %s",
        ", ".join(f"{d.code}({'on' if d.enabled else 'off'})" for d in departments.values()),
    )
    return DepartmentRegistry(departments)
