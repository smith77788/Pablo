"""Живой прогон эталонного набора ИИ-продавца на настоящей модели.

    cd tg-manager && python deploy/scripts/ai_sales_eval.py [--repeat 3]

Берёт ситуации из tests/fixtures/sales_golden.json, задаёт каждую продавцу
через тот же путь, что и в боте (bot_sales_persona.generate_reply → единый
AI-шлюз → проверка ответа), и считает:
  • сколько ответов прошли проверку цен и скидок с первой попытки;
  • сколько пришлось переписать и сколько клиенту ушла запасная фраза;
  • какая модель ответила и средняя задержка.

Нужен хотя бы один ключ ИИ в окружении (ANTHROPIC_API_KEY, OPENAI_API_KEY,
OPENROUTER_API_KEY, GROQ_API_KEY или GEMINI_API_KEY). Каждый прогон — платные
запросы к модели; в CI скрипт не запускается. Сравнивайте итог до и после
правки промпта или смены модели.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


async def main(repeat: int) -> int:
    from services import ai_claude, ai_providers
    from services import bot_sales_persona as bsp

    if not (ai_claude.enabled() or ai_providers.configured_providers()):
        print("Нет ни одного ключа ИИ — прогонять не на чем.")
        return 2
    with open(os.path.join(_ROOT, "tests", "fixtures", "sales_golden.json"),
              encoding="utf-8") as f:
        golden = json.load(f)
    cat = golden["catalog"]
    persona = dict(cat["persona"], fallback="__FALLBACK__")
    stats: Counter = Counter()
    models: Counter = Counter()
    latency = []
    for case in golden["cases"]:
        for _ in range(repeat):
            dialog: dict = {}
            reply = await bsp.generate_reply(persona, cat["products"], dialog,
                                             case["customer"], delivery=cat["delivery"],
                                             promos=cat["promos"])
            meta = dialog.get("_ai_meta") or {}
            stats["всего"] += 1
            if reply == "__FALLBACK__":
                stats["запасная фраза"] += 1
                verdict = "запасная фраза"
            elif meta.get("guard") in ("price", "discount"):
                stats["переписан"] += 1
                verdict = f"переписан ({meta['guard']}: {meta.get('guard_detail')})"
            else:
                stats["с первой попытки"] += 1
                verdict = "ок"
            if meta.get("model"):
                models[f"{meta.get('provider')}/{meta.get('model')}"] += 1
            latency.append(int(meta.get("latency_ms") or 0))
            print(f"[{case['id']}] {verdict}: {reply[:120]!r}")
    total = stats["всего"] or 1
    print("\nИтог:")
    for k in ("с первой попытки", "переписан", "запасная фраза"):
        print(f"  {k}: {stats[k]} из {stats['всего']} ({100 * stats[k] // total}%)")
    print("  модели:", ", ".join(f"{m} ×{n}" for m, n in models.most_common()) or "нет")
    if latency:
        print(f"  средняя задержка: {sum(latency) // len(latency)} мс")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeat", type=int, default=1, help="прогонов на ситуацию")
    sys.exit(asyncio.run(main(max(1, ap.parse_args().repeat))))
