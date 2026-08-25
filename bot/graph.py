"""Microsoft Graph: Planner (задачи) + SharePoint группы (файлы). Client credentials flow.

Мультиплановый: план (отдел-исполнитель) приходит в методы параметром, а не из глобального
env. Кеши сегментов/групп/имён — по plan_id.
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Config
from .constants import SHAREPOINT_ROOT_FOLDER

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"

# Имена сегментов меняются редко — держим их в кеше, чтобы не ходить в Graph на каждый просмотр
BUCKET_CACHE_TTL = 300

# Простым PUT грузим до 4 МБ, крупнее — upload session кусками (кратно 320 КБ)
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
CHUNK_SIZE = 10 * 320 * 1024  # 3.2 МБ

# SharePoint не принимает эти символы в имени файла
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    cleaned = _BAD_FILENAME_CHARS.sub("_", (name or "").strip()).strip(". ")
    return (cleaned or "file")[:120]


class GraphError(Exception):
    """Ошибка обращения к Graph, с которой заявку сохранить не удалось."""


class PlannerClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # всё, что зависит от плана, — кешируем по plan_id
        self._bucket_ids: dict[str, str] = {}
        self._group_ids: dict[str, str] = {}
        self._bucket_names_cache: dict[str, tuple[dict[str, str], float]] = {}
        self._user_ids: dict[str, str | None] = {}  # email -> AAD id (или None, если не найден)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- auth ---------------------------------------------------------------

    async def _get_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token

            url = f"https://login.microsoftonline.com/{self.cfg.graph_tenant_id}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self.cfg.graph_client_id,
                "client_secret": self.cfg.graph_client_secret,
                "scope": SCOPE,
            }
            client = await self._http()
            try:
                resp = await client.post(url, data=data)
            except httpx.HTTPError as exc:
                raise GraphError(f"не удалось получить токен: {exc}") from exc

            if resp.status_code != 200:
                log.error(
                    "Не удалось получить токен Graph (%s): %s. "
                    "Проверь GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET.",
                    resp.status_code,
                    resp.text[:500],
                )
                raise GraphError(f"ошибка получения токена: HTTP {resp.status_code}")

            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
            log.info("Токен Graph получен, истекает через %s c", payload.get("expires_in"))
            return self._token

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._get_token()}",
            "Content-Type": "application/json",
        }

    def _explain(self, resp: httpx.Response, action: str) -> GraphError:
        body = resp.text[:800]
        if resp.status_code in (401, 403):
            log.error(
                "Graph %s: %s %s — доступ запрещён. Проверь права приложения (тип Application) "
                "и admin consent в Entra: Tasks.ReadWrite.All — задачи Planner, "
                "Group.ReadWrite.All — группа плана, Sites.ReadWrite.All — файлы в SharePoint, "
                "User.Read.All — назначение исполнителей. Также проверь, что приложение имеет "
                "доступ к нужной группе плана. Ответ: %s",
                action,
                resp.status_code,
                resp.reason_phrase,
                body,
            )
        else:
            log.error("Graph %s: HTTP %s. Ответ: %s", action, resp.status_code, body)
        return GraphError(f"{action}: HTTP {resp.status_code}")

    # --- planner: сегменты --------------------------------------------------

    async def _resolve_bucket_id(
        self, plan_id: str, bucket_id_hint: str, bucket_name_hint: str
    ) -> str:
        if bucket_id_hint:
            return bucket_id_hint
        if plan_id in self._bucket_ids:
            return self._bucket_ids[plan_id]

        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/planner/plans/{plan_id}/buckets", headers=await self._headers()
        )
        if resp.status_code != 200:
            raise self._explain(resp, "получение списка bucket'ов плана")

        buckets = resp.json().get("value", [])
        if not buckets:
            raise GraphError("в плане нет ни одного bucket — создай хотя бы один в Planner")

        # Порядок bucket'ов в ответе Graph не совпадает с интерфейсом Planner — ищем по имени.
        wanted = (bucket_name_hint or "").strip().casefold()
        if wanted:
            for bucket in buckets:
                if (bucket.get("name") or "").strip().casefold() == wanted:
                    self._bucket_ids[plan_id] = bucket["id"]
                    log.info(
                        "План %s: bucket %r (%s) — найден по имени", plan_id, bucket.get("name"),
                        bucket["id"],
                    )
                    return bucket["id"]
            log.warning(
                "План %s: bucket %r не найден (есть: %s). Беру первый.",
                plan_id,
                bucket_name_hint,
                ", ".join(repr(b.get("name")) for b in buckets),
            )

        self._bucket_ids[plan_id] = buckets[0]["id"]
        log.warning("План %s: использую первый bucket %r", plan_id, buckets[0].get("name"))
        return buckets[0]["id"]

    async def bucket_names(self, plan_id: str) -> dict[str, str]:
        """id сегмента -> имя для плана. Кешируем на BUCKET_CACHE_TTL."""
        cached = self._bucket_names_cache.get(plan_id)
        if cached and time.monotonic() - cached[1] < BUCKET_CACHE_TTL:
            return cached[0]

        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/planner/plans/{plan_id}/buckets", headers=await self._headers()
        )
        if resp.status_code != 200:
            raise self._explain(resp, "чтение сегментов плана")

        names = {b["id"]: b["name"] for b in resp.json().get("value", [])}
        self._bucket_names_cache[plan_id] = (names, time.monotonic())
        return names

    async def list_plan_tasks(self, plan_id: str) -> dict[str, dict]:
        """Все задачи плана одним запросом: task_id -> задача (с пагинацией по nextLink)."""
        client = await self._http()
        url = f"{GRAPH_BASE}/planner/plans/{plan_id}/tasks"
        result: dict[str, dict] = {}
        while url:
            resp = await client.get(url, headers=await self._headers())
            if resp.status_code != 200:
                raise self._explain(resp, "чтение задач плана")
            data = resp.json()
            for task in data.get("value", []):
                result[task["id"]] = task
            url = data.get("@odata.nextLink")
        return result

    # --- planner: исполнители ----------------------------------------------

    async def _resolve_user_id(self, email: str) -> str | None:
        """email -> AAD object id. None, если не нашли/нет прав (заявку не роняем)."""
        email = email.strip()
        if not email:
            return None
        if email in self._user_ids:
            return self._user_ids[email]

        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/users/{quote(email)}?$select=id", headers=await self._headers()
        )
        if resp.status_code == 200:
            self._user_ids[email] = resp.json().get("id")
        else:
            log.error(
                "Не удалось найти пользователя %s (HTTP %s) — задача создастся без назначения. "
                "Для назначения нужно право User.Read.All (Application) + admin consent.",
                email,
                resp.status_code,
            )
            self._user_ids[email] = None
        return self._user_ids[email]

    async def _assignments(self, emails: tuple[str, ...] | list[str]) -> dict:
        assignments: dict[str, dict] = {}
        for email in emails or ():
            uid = await self._resolve_user_id(email)
            if uid:
                assignments[uid] = {
                    "@odata.type": "microsoft.graph.plannerAssignment",
                    "orderHint": " !",
                }
        return assignments

    # --- planner: задачи ----------------------------------------------------

    async def create_task(
        self,
        *,
        plan_id: str,
        bucket_id: str = "",
        bucket_name: str = "",
        title: str,
        due_date_iso: str | None,
        priority: int,
        applied_categories: dict[str, bool] | None = None,
        assignees: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, str]:
        """Создаёт задачу в указанном плане (без details). Возвращает {"id","url"}."""
        client = await self._http()
        resolved_bucket = await self._resolve_bucket_id(plan_id, bucket_id, bucket_name)

        body: dict[str, object] = {
            "planId": plan_id,
            "bucketId": resolved_bucket,
            "title": title[:255],
            "priority": priority,
        }
        if due_date_iso:
            body["dueDateTime"] = due_date_iso
        if applied_categories:
            body["appliedCategories"] = applied_categories
        if assignees:
            assignments = await self._assignments(assignees)
            if assignments:
                body["assignments"] = assignments

        resp = await client.post(
            f"{GRAPH_BASE}/planner/tasks", headers=await self._headers(), json=body
        )
        if resp.status_code not in (200, 201):
            raise self._explain(resp, "создание задачи")

        task_id = resp.json()["id"]
        log.info("Задача Planner создана: %s (план %s)", task_id, plan_id)
        return {
            "id": task_id,
            "url": f"https://tasks.office.com/{self.cfg.graph_tenant_id}/Home/Task/{task_id}",
        }

    async def get_task(self, task_id: str) -> dict | None:
        """Задача целиком или None, если её больше нет. task_id глобален (план не нужен)."""
        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/planner/tasks/{task_id}", headers=await self._headers()
        )
        if resp.status_code == 404:
            log.info("Задача %s не найдена в Planner (удалена?)", task_id)
            return None
        if resp.status_code != 200:
            raise self._explain(resp, "чтение задачи")
        return resp.json()

    async def set_details(
        self,
        task_id: str,
        description: str,
        references: list[tuple[str, str]] | None = None,
    ) -> None:
        """Пишет описание и ссылки-вложения задачи. references — список (webUrl, alias)."""
        client = await self._http()
        url = f"{GRAPH_BASE}/planner/tasks/{task_id}/details"

        resp = await client.get(url, headers=await self._headers())
        if resp.status_code != 200:
            raise self._explain(resp, "чтение details задачи")

        etag = resp.json().get("@odata.etag")
        headers = await self._headers()
        headers["If-Match"] = etag

        payload: dict[str, object] = {"description": description}
        if references:
            payload["references"] = {
                _reference_key(web_url): {
                    "@odata.type": "microsoft.graph.plannerExternalReference",
                    "alias": alias,
                    "type": "Other",
                    "previewPriority": " !",
                }
                for web_url, alias in references
            }

        resp = await client.patch(url, headers=headers, json=payload)
        if resp.status_code not in (200, 204):
            raise self._explain(resp, "запись details задачи (описание/вложения)")

    # --- SharePoint ---------------------------------------------------------

    async def _resolve_group_id(self, plan_id: str) -> str:
        """groupId группы плана — в её библиотеке лежат файлы. Кеш по plan_id."""
        if plan_id in self._group_ids:
            return self._group_ids[plan_id]

        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/planner/plans/{plan_id}", headers=await self._headers()
        )
        if resp.status_code != 200:
            raise self._explain(resp, "чтение плана (определение группы)")

        plan = resp.json()
        group_id = plan.get("owner") or (plan.get("container") or {}).get("containerId")
        if not group_id:
            raise GraphError("не удалось определить группу плана (owner пуст)")

        self._group_ids[plan_id] = group_id
        log.info("Группа плана %s: %s", plan_id, group_id)
        return group_id

    async def upload_file(
        self, local_path: Path, *, plan_id: str, ticket_number: str, file_name: str
    ) -> str:
        """Загружает файл в библиотеку группы плана и возвращает webUrl.

        Путь: <SHAREPOINT_ROOT_FOLDER>/<номер заявки>/<имя файла>.
        """
        group_id = await self._resolve_group_id(plan_id)
        safe_name = sanitize_filename(file_name)
        item_path = quote(f"{SHAREPOINT_ROOT_FOLDER}/{ticket_number}/{safe_name}")
        base = f"{GRAPH_BASE}/groups/{group_id}/drive/root:/{item_path}"
        size = local_path.stat().st_size

        if size <= SIMPLE_UPLOAD_LIMIT:
            return await self._upload_simple(base, local_path)
        return await self._upload_session(base, local_path, size)

    async def _upload_simple(self, base: str, local_path: Path) -> str:
        client = await self._http()
        headers = await self._headers()
        headers["Content-Type"] = "application/octet-stream"

        resp = await client.put(
            f"{base}:/content?@microsoft.graph.conflictBehavior=rename",
            headers=headers,
            content=local_path.read_bytes(),
            timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise self._explain(resp, "загрузка файла в SharePoint")
        return resp.json().get("webUrl", "")

    async def _upload_session(self, base: str, local_path: Path, size: int) -> str:
        """Крупные файлы — кусками, чтобы не держать всё в памяти."""
        client = await self._http()
        resp = await client.post(
            f"{base}:/createUploadSession",
            headers=await self._headers(),
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
        )
        if resp.status_code not in (200, 201):
            raise self._explain(resp, "создание upload session")

        upload_url = resp.json()["uploadUrl"]
        with local_path.open("rb") as fh:
            start = 0
            while start < size:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                r = await client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                    timeout=180,
                )
                if r.status_code in (200, 201):
                    return r.json().get("webUrl", "")
                if r.status_code != 202:
                    raise self._explain(r, "загрузка куска файла в SharePoint")
                start = end + 1

        raise GraphError("upload session завершилась без итогового ответа")


def _reference_key(web_url: str) -> str:
    """Planner использует URL как ключ словаря references и требует экранирования."""
    return (
        web_url.replace("%", "%25")
        .replace(".", "%2E")
        .replace(":", "%3A")
        .replace("@", "%40")
        .replace("#", "%23")
    )
