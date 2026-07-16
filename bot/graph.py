"""Microsoft Graph: Planner (задачи) + SharePoint группы (файлы). Client credentials flow."""

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
        self._bucket_id: str | None = cfg.planner_bucket_id or None
        self._group_id: str | None = None

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
            # обновляем за 60 секунд до истечения
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
                "Group.ReadWrite.All — группа плана, Sites.ReadWrite.All — файлы в SharePoint. "
                "Также проверь, что приложение имеет доступ к группе плана %s. Ответ: %s",
                action,
                resp.status_code,
                resp.reason_phrase,
                self.cfg.planner_plan_id,
                body,
            )
        else:
            log.error("Graph %s: HTTP %s. Ответ: %s", action, resp.status_code, body)
        return GraphError(f"{action}: HTTP {resp.status_code}")

    # --- planner ------------------------------------------------------------

    async def _resolve_bucket_id(self) -> str:
        if self._bucket_id:
            return self._bucket_id

        client = await self._http()
        url = f"{GRAPH_BASE}/planner/plans/{self.cfg.planner_plan_id}/buckets"
        resp = await client.get(url, headers=await self._headers())
        if resp.status_code != 200:
            raise self._explain(resp, "получение списка bucket'ов плана")

        buckets = resp.json().get("value", [])
        if not buckets:
            raise GraphError("в плане нет ни одного bucket — создай хотя бы один в Planner")

        # Порядок bucket'ов в ответе Graph не совпадает с порядком в интерфейсе Planner,
        # поэтому «первый» — это лотерея (у нас это оказалось «Завершено»).
        # Сначала ищем bucket по имени, и только если не нашли — берём первый.
        wanted = self.cfg.planner_bucket_name.strip().casefold()
        if wanted:
            for bucket in buckets:
                if (bucket.get("name") or "").strip().casefold() == wanted:
                    self._bucket_id = bucket["id"]
                    log.info(
                        "Использую bucket %r (%s) — найден по PLANNER_BUCKET_NAME",
                        bucket.get("name"),
                        self._bucket_id,
                    )
                    return self._bucket_id
            log.warning(
                "Bucket с именем %r в плане не найден (есть: %s). Беру первый — проверь "
                "PLANNER_BUCKET_NAME или задай PLANNER_BUCKET_ID явно.",
                self.cfg.planner_bucket_name,
                ", ".join(repr(b.get("name")) for b in buckets),
            )

        self._bucket_id = buckets[0]["id"]
        log.warning(
            "Использую первый bucket плана: %r (%s)", buckets[0].get("name"), self._bucket_id
        )
        return self._bucket_id

    async def create_task(
        self,
        *,
        title: str,
        due_date_iso: str | None,
        priority: int,
        applied_categories: dict[str, bool] | None = None,
    ) -> dict[str, str]:
        """Создаёт задачу в Planner (без details).

        Описание и вложения пишутся отдельно через set_details — к тому моменту уже
        известен результат загрузки файлов в SharePoint, и details пишутся ровно один раз.

        Возвращает {"id": ..., "url": ...}. Бросает GraphError при неудаче.
        """
        client = await self._http()
        bucket_id = await self._resolve_bucket_id()

        body: dict[str, object] = {
            "planId": self.cfg.planner_plan_id,
            "bucketId": bucket_id,
            "title": title[:255],
            "priority": priority,
        }
        if due_date_iso:
            body["dueDateTime"] = due_date_iso
        if applied_categories:
            body["appliedCategories"] = applied_categories

        resp = await client.post(
            f"{GRAPH_BASE}/planner/tasks", headers=await self._headers(), json=body
        )
        if resp.status_code not in (200, 201):
            raise self._explain(resp, "создание задачи")

        task_id = resp.json()["id"]
        log.info("Задача Planner создана: %s", task_id)
        return {
            "id": task_id,
            "url": f"https://tasks.office.com/{self.cfg.graph_tenant_id}/Home/Task/{task_id}",
        }

    async def get_task(self, task_id: str) -> dict | None:
        """Задача целиком или None, если её больше нет. Бросает GraphError на прочих ошибках."""
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

    async def _resolve_group_id(self) -> str:
        """groupId группы, которой принадлежит план — в её библиотеке лежат файлы."""
        if self._group_id:
            return self._group_id

        client = await self._http()
        resp = await client.get(
            f"{GRAPH_BASE}/planner/plans/{self.cfg.planner_plan_id}", headers=await self._headers()
        )
        if resp.status_code != 200:
            raise self._explain(resp, "чтение плана (определение группы)")

        plan = resp.json()
        group_id = plan.get("owner") or (plan.get("container") or {}).get("containerId")
        if not group_id:
            raise GraphError("не удалось определить группу плана (owner пуст)")

        self._group_id = group_id
        log.info("Группа плана: %s", group_id)
        return group_id

    async def upload_file(self, local_path: Path, *, ticket_number: str, file_name: str) -> str:
        """Загружает файл в библиотеку группы и возвращает webUrl.

        Путь: <SHAREPOINT_ROOT_FOLDER>/<номер заявки>/<имя файла> — материалы одной
        заявки лежат вместе. Бросает GraphError.
        """
        group_id = await self._resolve_group_id()
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
        # upload session — предавторизованный URL, токен в заголовках не нужен
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
