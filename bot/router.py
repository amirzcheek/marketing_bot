"""Маршрутизация заявки: по коду отдела-исполнителя отдаёт, куда её создавать и слать."""

import logging
from dataclasses import dataclass

from .constants import BUCKET_STATUS
from .departments import Department, DepartmentRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    code: str
    name: str
    plan_id: str
    bucket_id: str
    bucket_name: str
    notification_chat_id: str
    assignees: tuple[str, ...]
    bucket_status: dict[str, str]


class TicketRouter:
    def __init__(self, registry: DepartmentRegistry):
        self.registry = registry

    def _route(self, dep: Department) -> Route:
        return Route(
            code=dep.code,
            name=dep.name,
            plan_id=dep.planner_plan_id,
            bucket_id=dep.planner_bucket_id,
            bucket_name=dep.planner_bucket_name,
            notification_chat_id=dep.notification_chat_id,
            assignees=dep.assignees,
            # у отдела свой маппинг сегмент→статус; если нет — общий дефолт
            bucket_status=dep.bucket_status or BUCKET_STATUS,
        )

    def route(self, code: str) -> Route | None:
        dep = self.registry.get(code)
        return self._route(dep) if dep else None

    def targets(self) -> list[Route]:
        """Включённые отделы-исполнители для кнопок выбора получателя."""
        return [self._route(d) for d in self.registry.enabled()]
