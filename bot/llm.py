"""Нормализация описания заявки через LLM (OpenAI-совместимый эндпоинт, Qwen3 на OVMS).

Нормализация опциональна: любая ошибка/таймаут не должны блокировать заявку.
"""

import json
import logging
import re

import httpx

from .config import Config
from .constants import TASK_TYPES

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

NORMALIZE_PROMPT = (
    "Ты — редактор заявок в отдел маркетинга университета. "
    "Перепиши описание задачи кратко и по-деловому: убери воду, вежливые обороты и повторы, "
    "сохрани все конкретные факты (что нужно сделать, для чего, формат, размеры, даты, места, имена). "
    "Пиши на русском языке, 1-4 предложения или короткий список. "
    "Ничего не выдумывай и не добавляй от себя. "
    "Верни ТОЛЬКО очищенный текст, без пояснений, без кавычек, без заголовков."
)

SUGGEST_PROMPT = (
    "Ты классифицируешь заявки в отдел маркетинга университета. "
    "Доступные типы задач: {types}. "
    "Определи наиболее подходящий тип по описанию заявки. "
    'Верни ТОЛЬКО JSON вида {{"type": "<один из типов или null>", "confidence": <0..1>}}. '
    "Без пояснений и без markdown."
)


def _clean(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    return text.strip().strip('"').strip()


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.llm_normalize and self.cfg.llm_base_url and self.cfg.llm_model)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.llm_timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _chat(self, system: str, user: str, max_tokens: int = 512) -> str | None:
        payload = {
            "model": self.cfg.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            # Qwen3: отключаем режим рассуждений — нужен только чистый ответ
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key and self.cfg.llm_api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self.cfg.llm_api_key}"

        client = await self._http()
        try:
            resp = await client.post(
                f"{self.cfg.llm_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            log.warning("LLM недоступен или вернул неожиданный ответ: %s", exc)
            return None

    async def normalize(self, description: str) -> str | None:
        """Возвращает нормализованный текст или None, если LLM не сработал."""
        if not self.enabled or not description.strip():
            return None
        raw = await self._chat(NORMALIZE_PROMPT, description.strip())
        if raw is None:
            return None
        cleaned = _clean(raw)
        if not cleaned or len(cleaned) > len(description) * 3:
            # мусорный ответ — лучше оставить оригинал
            return None
        return cleaned

    async def suggest_type(self, description: str) -> str | None:
        """Подсказка типа задачи. Только подсказка, ничего не навязываем."""
        if not self.enabled or not description.strip():
            return None
        known = [name for _, name in TASK_TYPES.values() if name]
        raw = await self._chat(
            SUGGEST_PROMPT.format(types=", ".join(known)),
            description.strip(),
            max_tokens=128,
        )
        if raw is None:
            return None
        text = _clean(raw)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        suggested = data.get("type")
        confidence = data.get("confidence")
        if not isinstance(suggested, str) or suggested not in known:
            return None
        if isinstance(confidence, (int, float)) and confidence < 0.5:
            return None
        return suggested
