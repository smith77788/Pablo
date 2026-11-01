"""Единая точка обращения к ИИ для разговоров и подсказок.

ЗАЧЕМ. До этого модуля каждый потребитель звал модели по-своему: продавец в
боте сам перебирал OpenAI-совместимых провайдеров, AI-автоответ ходил прямо в
api.openai.com с зашитой gpt-4o-mini, а контент — через spintax_ai.complete.
Отсюда три беды: лимиты бесплатных моделей (services/llm_gate) видел только
контент, расход нельзя было посчитать по владельцу, а живой клиент получал
«дайте секунду…» всякий раз, когда бесплатная модель упиралась в квоту.

Шлюз делает три вещи:
  • МАРШРУТ ПО ЗАДАЧЕ. TASK_CUSTOMER (ответ живому клиенту) идёт сперва на
    надёжную модель: Claude, затем платные OpenAI-совместимые, бесплатные — в
    запас. TASK_BACKGROUND (выжимки, разметка) — наоборот, сперва бесплатные:
    там задержка в минуту ничего не стоит.
  • ЛИМИТЫ. Перед запросом модель сверяется с паузами llm_gate; 429 ставит
    паузу, а не повторяется на каждом сообщении.
  • УЧЁТ НА ВЛАДЕЛЬЦА. llm_owner_usage: вызовы, удачные ответы и токены по
    владельцу, задаче и дню — основа для лимита по тарифу.

Ответ — AiReply; text=None значит «никто не ответил», причины в errors.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from services import llm_gate

log = logging.getLogger(__name__)

TASK_CUSTOMER = "customer"      # реплика живому клиенту — надёжность важнее цены
TASK_ASSISTANT = "assistant"    # помощник владельца
TASK_BACKGROUND = "background"  # выжимки и разметка — сперва бесплатные модели
TASKS = (TASK_CUSTOMER, TASK_ASSISTANT, TASK_BACKGROUND)

# Провайдеры, у которых запрос стоит денег, а значит лимиты не «по минутам на
# бесплатной квоте». Для ответа клиенту они идут раньше бесплатных.
_PAID_PROVIDERS = ("openai",)

_POOL: dict[str, Any] = {"pool": None}


def attach(pool) -> None:
    """Пул БД для учёта расхода. Без него шлюз работает, но не считает."""
    _POOL["pool"] = pool


def _pool():
    # Отдельно шлюз подключают редко: тот же пул уже держит llm_gate (его
    # цепляют и HTTP-слой, и фоновый цикл), поэтому берём его, если своего нет.
    return _POOL.get("pool") or llm_gate._STATE.get("pool")


@dataclass
class AiReply:
    text: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    providers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.text)


def _is_free(model: str) -> bool:
    return model.endswith(":free")


def order_candidates(providers: list, task: str, *, prefer_provider: str = "",
                     prefer_model: str = "") -> list[tuple[Any, str]]:
    """Порядок попыток (провайдер, модель) для задачи. ЧИСТАЯ.

    Явный выбор владельца (провайдер/модель в настройках персоны) — всегда
    первым. Дальше для ответа клиенту: платные провайдеры, затем модели без
    «:free», затем бесплатные; для фоновых задач — бесплатные впереди."""
    pref = (prefer_provider or "").strip().lower()
    out: list[tuple[Any, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(prov, model):
        key = (prov.name, model)
        if model and key not in seen:
            seen.add(key)
            out.append((prov, model))

    for prov in providers:
        if pref and prov.name.lower() == pref and prefer_model:
            add(prov, prefer_model)
    rest: list[tuple[Any, str]] = []
    for prov in providers:
        models = list(prov.models or [])
        if prefer_model and prov.name.lower() == pref:
            models = [prefer_model] + [m for m in models if m != prefer_model]
        for m in models[:4]:
            rest.append((prov, m))

    def rank(item):
        prov, model = item
        chosen = 0 if (pref and prov.name.lower() == pref) else 1
        paid = 0 if prov.name in _PAID_PROVIDERS else 1
        free = 1 if _is_free(model) else 0
        if task == TASK_BACKGROUND:
            return (chosen, -free, paid)
        return (chosen, paid, free)

    for prov, model in sorted(rest, key=rank):
        add(prov, model)
    return out


async def _record(owner_id: Optional[int], task: str, reply: AiReply) -> None:
    """Учёт вызова на владельца. Сбой записи не мешает ответу, но и не молчит."""
    pool = _pool()
    if pool is None or not owner_id:
        return
    try:
        await pool.execute(
            "INSERT INTO llm_owner_usage (day, owner_id, task, calls, ok, "
            "input_tokens, output_tokens) VALUES (CURRENT_DATE, $1, $2, 1, $3, $4, $5) "
            "ON CONFLICT (day, owner_id, task) DO UPDATE SET "
            "calls = llm_owner_usage.calls + 1, ok = llm_owner_usage.ok + EXCLUDED.ok, "
            "input_tokens = llm_owner_usage.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = llm_owner_usage.output_tokens + EXCLUDED.output_tokens",
            int(owner_id), task, 1 if reply.ok else 0,
            int(reply.input_tokens), int(reply.output_tokens))
    except Exception as exc:  # noqa: BLE001 - учёт не должен ронять ответ клиенту
        log.warning("ai_gateway: учёт расхода не записан (owner=%s): %s", owner_id, exc)


async def _try_claude(system: str, history: list[dict], max_tokens: int,
                      reply: AiReply) -> bool:
    from services import ai_claude
    if not ai_claude.enabled():
        return False
    reply.providers.append("anthropic")
    model = ai_claude.CHAT_MODEL
    if await llm_gate.blocked_until("anthropic", model):
        reply.errors.append(f"anthropic/{model}: на паузе по лимиту")
        return False
    try:
        text, usage = await ai_claude.chat(system, history, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - failover на следующего провайдера
        if not await llm_gate.note_limit("anthropic", model, exc):
            await llm_gate.note_call("anthropic", ok=False)
        reply.errors.append(f"anthropic/{model}: {type(exc).__name__}: {str(exc)[:140]}")
        return False
    await llm_gate.note_ok("anthropic", model)
    reply.text, reply.provider, reply.model = text, "anthropic", model
    reply.input_tokens = usage.get("input_tokens", 0)
    reply.output_tokens = usage.get("output_tokens", 0)
    return True


async def _try_openai_compatible(messages: list[dict], task: str, max_tokens: int,
                                 temperature: float, prefer_provider: str,
                                 prefer_model: str, timeout: float,
                                 reply: AiReply) -> bool:
    from services import ai_providers
    providers = ai_providers.configured_providers()
    for p in providers:
        if p.name not in reply.providers:
            reply.providers.append(p.name)
    if not providers:
        return False
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        reply.errors.append(f"openai lib: {exc}")
        return False
    clients: dict[str, Any] = {}
    for prov, model in order_candidates(providers, task, prefer_provider=prefer_provider,
                                        prefer_model=prefer_model):
        if await llm_gate.blocked_until(prov.name, model):
            reply.errors.append(f"{prov.name}/{model}: на паузе по лимиту")
            continue
        try:
            client = clients.get(prov.name)
            if client is None:
                client = AsyncOpenAI(api_key=prov.api_key, base_url=prov.base_url,
                                     timeout=timeout)
                clients[prov.name] = client
            resp = await client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens,
                temperature=temperature)
            choice = resp.choices[0]
            text = (choice.message.content or "").strip()
            if not text:
                await llm_gate.note_call(prov.name, ok=False)
                reply.errors.append(f"{prov.name}/{model}: пустой ответ")
                continue
        except Exception as exc:  # noqa: BLE001 - failover по моделям и провайдерам
            if not await llm_gate.note_limit(prov.name, model, exc):
                await llm_gate.note_call(prov.name, ok=False)
            reply.errors.append(f"{prov.name}/{model}: {type(exc).__name__}: {str(exc)[:140]}")
            continue
        await llm_gate.note_ok(prov.name, model)
        usage = getattr(resp, "usage", None)
        reply.text, reply.provider, reply.model = text, prov.name, model
        reply.input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        reply.output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return True
    return False


async def chat(messages: list[dict], *, task: str = TASK_CUSTOMER,
               owner_id: Optional[int] = None, max_tokens: int = 400,
               temperature: float = 0.7, prefer_provider: str = "",
               prefer_model: str = "", timeout: float = 25.0) -> AiReply:
    """Один ответ модели на диалог в формате OpenAI ([{role, content}], первым —
    system). Никогда не кидает: неудача — AiReply с text=None и причинами."""
    if task not in TASKS:
        raise ValueError(f"неизвестная задача ИИ: {task}")
    started = time.monotonic()
    reply = AiReply()
    system = "\n\n".join(str(m.get("content") or "") for m in messages
                         if m.get("role") == "system")
    history = [m for m in messages if m.get("role") != "system"]
    try:
        done = False
        # Явный выбор владельца уважаем: если он назвал не-Anthropic провайдера,
        # Claude идёт в запас, а не первым.
        claude_first = task != TASK_BACKGROUND and (
            not prefer_provider or prefer_provider.lower() in ("anthropic", "claude"))
        if claude_first:
            done = await _try_claude(system, history, max_tokens, reply)
        if not done:
            done = await _try_openai_compatible(
                messages, task, max_tokens, temperature, prefer_provider,
                prefer_model, timeout, reply)
        if not done and not claude_first:
            await _try_claude(system, history, max_tokens, reply)
    except Exception as exc:  # noqa: BLE001 - контракт «никогда не кидает»
        reply.errors.append(f"шлюз: {type(exc).__name__}: {str(exc)[:140]}")
    reply.latency_ms = int((time.monotonic() - started) * 1000)
    await _record(owner_id, task, reply)
    return reply


async def owner_usage(pool, owner_id: int, *, days: int = 30) -> list[dict]:
    """Расход ИИ владельца по дням и задачам за последние days дней."""
    days = max(1, min(int(days or 30), 366))
    rows = await pool.fetch(
        "SELECT day, task, calls, ok, input_tokens, output_tokens "
        "FROM llm_owner_usage WHERE owner_id=$1 AND day >= CURRENT_DATE - $2::int "
        "ORDER BY day DESC, task", int(owner_id), days)
    return [dict(r) for r in rows]
