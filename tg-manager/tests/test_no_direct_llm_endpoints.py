"""Храповик: модели ИИ зовутся только через единый шлюз (services/ai_gateway).

ЗАЧЕМ. Шлюз держит три вещи, которых нет у прямого запроса: дневной лимит ИИ
по тарифу владельца, паузы моделей, упёршихся в квоту (llm_gate), и учёт
расхода на владельца. SEO-генерация в боте ходила прямо в openrouter.ai: у
бесплатного тарифа не было потолка, на исчерпанной квоте каждое нажатие снова
получало 429, а без OPENROUTER_API_KEY функция молчала, даже когда подключены
другие провайдеры.

Проверка статическая: строковый литерал с адресом API модели (не докстринг и
не комментарий) допустим только в модулях самого шлюза и провайдеров.
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOSTS = (
    "api.openai.com", "api.anthropic.com", "openrouter.ai", "api.groq.com",
    "api.deepseek.com", "api.mistral.ai", "api.together.xyz",
)
# Шлюз и каталог провайдеров — место, где адреса и должны жить. va_image —
# генерация картинок: у шлюза нет такой задачи.
ALLOWED = {
    os.path.join("services", "ai_gateway.py"),
    os.path.join("services", "ai_providers.py"),
    os.path.join("services", "va_image.py"),
}
_SKIP_DIRS = {"tests", ".venv", "venv", "node_modules", "__pycache__", ".git"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                out.add(id(body[0].value))
    return out


def find_violations() -> list[str]:
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, ROOT)
            if rel in ALLOWED:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            docs = _docstring_nodes(tree)
            for n in ast.walk(tree):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and id(n) not in docs
                        and any(h in n.value for h in HOSTS)):
                    bad.append(f"{rel}:{n.lineno}")
    return sorted(bad)


def test_llm_endpoints_only_in_the_gateway():
    bad = find_violations()
    assert not bad, (
        "Прямой запрос к API модели мимо services/ai_gateway — без лимита по "
        "тарифу, пауз llm_gate и учёта расхода. Зови ai_gateway.chat/complete:\n  "
        + "\n  ".join(bad))


def test_detector_sees_a_planted_call(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text(
        '"""Ходили в openrouter.ai — теперь нет."""\n'
        "async def f(http):\n"
        '    """api.openai.com упомянут в докстринге — не нарушение."""\n'
        '    return await http.post("https://openrouter.ai/api/v1/chat/completions")\n',
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", str(tmp_path))
    assert find_violations() == ["mod.py:4"]


async def test_seo_generation_goes_through_the_gateway(monkeypatch):
    from bot.handlers import seo
    from services import ai_gateway

    seen = {}

    async def _complete(prompt, **kw):
        seen.update(kw)
        return '```json\n{"title": "Кофе", "about": "о", "username": "", "reasoning": "r"}\n```'

    monkeypatch.setattr(ai_gateway, "complete", _complete)
    out = await seo._ai_generate_seo(
        None, title="t", about="", username="", entity_type="Telegram channel",
        keywords=["кофе"], owner_id=42)
    assert out and out["title"] == "Кофе"
    assert seen.get("owner_id") == 42, "расход ИИ не привязан к владельцу"


async def test_seo_generation_survives_garbage_and_silence(monkeypatch):
    from bot.handlers import seo
    from services import ai_gateway

    for answer in (None, "", "не json", "[1, 2]"):
        async def _complete(prompt, _a=answer, **kw):
            return _a

        monkeypatch.setattr(ai_gateway, "complete", _complete)
        assert await seo._ai_generate_seo(
            None, title="t", about="", username="", entity_type="x",
            keywords=[], owner_id=1) is None
