"""Иллюстрации к постам виртуального администратора через УЖЕ настроенные
ИИ-провайдеры (services.ai_providers).

Владелец: «если к посту по смыслу нужно изображение — добавляем, если нет —
пропускаем». Поэтому:
  1. decide_illustration() спрашивает текстовую модель (ai_claude.complete):
     нужна ли посту картинка и какой у неё короткий промпт;
  2. generate_image_bytes() генерирует картинку через первый доступный
     image-способный провайдер (OpenAI Images, затем Gemini/Imagen).

Доставка. В операцию кладём ЛЁГКИЙ текстовый media_prompt, а не саму картинку:
op_worker генерирует байты в момент публикации и переотправляет их через
Telethon (тот же путь, что и для загруженных фото). Картинку в params не кладём
— это раздуло бы operation_queue.

Fail-open на каждом шаге: нет провайдера/ключа, плохой ответ или сбой сети —
просто без картинки, публикацию НЕ роняем (пост уходит текстом, как и раньше).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

log = logging.getLogger(__name__)

_DECIDE_SYS = (
    "Ты — выпускающий редактор Telegram-канала. По тексту поста реши, нужна ли "
    "ему иллюстрация, чтобы он выглядел живее и уместнее. Иллюстрация НУЖНА "
    "анонсам, обзорам, подборкам, новостям о продукте, визуальным темам. Она НЕ "
    "нужна коротким заметкам, цитатам, вопросам, опросам, чисто текстовым "
    "рассуждениям. Ответь СТРОГО одним JSON-объектом без пояснений: "
    '{"need": true|false, "prompt": "<короткое англоязычное описание картинки '
    'для генератора, без текста на изображении>"}. Если картинка не нужна — '
    '{"need": false, "prompt": ""}.'
)

_IMG_SIZE = os.getenv("VA_IMAGE_SIZE", "1024x1024")


def _extract_json(raw: str) -> dict:
    """Вытащить первый JSON-объект из ответа модели (она любит обрамлять текстом)."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


async def decide_illustration(text: str, pillar: str = "") -> tuple[bool, str]:
    """Нужна ли посту картинка и её промпт. Fail-open → (False, "")."""
    text = (text or "").strip()
    if len(text) < 20:
        return False, ""
    try:
        from services import ai_claude
        raw = await ai_claude.complete(
            _DECIDE_SYS,
            f"Рубрика: {pillar or '—'}\n\nПост:\n{text[:1500]}",
            timeout=40.0,
        )
    except Exception:
        log.debug("va_image.decide_illustration: модель недоступна", exc_info=True)
        return False, ""
    data = _extract_json(raw)
    need = bool(data.get("need"))
    prompt = str(data.get("prompt") or "").strip()[:400]
    return (need and bool(prompt)), prompt


async def _openai_image(provider, prompt: str) -> bytes | None:
    """OpenAI Images API ({base}/images/generations), ответ b64_json → bytes."""
    import aiohttp
    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    url = provider.base_url.rstrip("/") + "/images/generations"
    body = {"model": model, "prompt": prompt, "n": 1, "size": _IMG_SIZE,
            "response_format": "b64_json"}
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                data = await r.json()
    except Exception:
        log.debug("va_image._openai_image: сбой запроса", exc_info=True)
        return None
    try:
        items = data.get("data") or []
        if not items:
            # gpt-image-1 на части аккаунтов недоступна → повторяем на dall-e-3.
            if model != "dall-e-3" and "model" in str(data.get("error", "")).lower():
                return await _openai_image_model(provider, prompt, "dall-e-3")
            return None
        b64 = items[0].get("b64_json")
        if b64:
            return base64.b64decode(b64)
        img_url = items[0].get("url")
        if img_url:
            return await _download(img_url)
    except Exception:
        log.debug("va_image._openai_image: разбор ответа", exc_info=True)
    return None


async def _openai_image_model(provider, prompt: str, model: str) -> bytes | None:
    import aiohttp
    url = provider.base_url.rstrip("/") + "/images/generations"
    body = {"model": model, "prompt": prompt, "n": 1, "size": _IMG_SIZE,
            "response_format": "b64_json"}
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                data = await r.json()
        b64 = (data.get("data") or [{}])[0].get("b64_json")
        return base64.b64decode(b64) if b64 else None
    except Exception:
        return None


async def _gemini_image(provider, prompt: str) -> bytes | None:
    """Gemini/Imagen через generativelanguage predict. base_url у провайдера —
    OpenAI-совместимый (…/v1beta/openai/); для картинок берём нативный v1beta."""
    import aiohttp
    base = provider.base_url.split("/v1beta")[0].rstrip("/")
    model = os.getenv("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
    url = (f"{base}/v1beta/models/{model}:predict?key={provider.api_key}")
    body = {"instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1}}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                data = await r.json()
        preds = data.get("predictions") or []
        if preds:
            b64 = preds[0].get("bytesBase64Encoded") or preds[0].get("image")
            if b64:
                return base64.b64decode(b64)
    except Exception:
        log.debug("va_image._gemini_image: сбой", exc_info=True)
    return None


async def _download(img_url: str) -> bytes | None:
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(img_url, timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        pass
    return None


async def generate_image_bytes(prompt: str) -> bytes | None:
    """Сгенерировать картинку по промпту первым доступным провайдером.

    Порядок: OpenAI Images → Gemini/Imagen. Fail-open → None (пост без картинки).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    try:
        from services import ai_providers
        provs = {p.name: p for p in ai_providers.configured_providers()}
    except Exception:
        return None
    p = provs.get("openai")
    if p:
        b = await _openai_image(p, prompt)
        if b:
            return b
    p = provs.get("gemini")
    if p:
        b = await _gemini_image(p, prompt)
        if b:
            return b
    return None
