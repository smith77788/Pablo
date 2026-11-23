"""`d.get("k", "").strip()` не защищает от `None`: умолчание срабатывает только
когда ключа НЕТ, а JSON с экрана и строки базы приносят ключ со значением
null. Так падала ручка создания автоответа — форма шлёт `keyword: null` у
правил «/start» и «любое сообщение», и такие правила не создавались вовсе.
Правильно: `(d.get("k") or "").strip()`."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAT = re.compile(r'\.get\(\s*["\'][\w]+["\']\s*,\s*["\']{2}\s*\)\s*\.(?:strip|lower|upper|split|lstrip|rstrip)\(')


def test_probe_catches_known_bad_shape():
    assert PAT.search('keyword = body.get("keyword", "").strip()')
    assert not PAT.search('keyword = (body.get("keyword") or "").strip()')


def test_no_get_default_then_strip_anywhere():
    bad = []
    for base in ("services", "bot"):
        for p in sorted((ROOT / base).glob("**/*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if PAT.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not bad, "\n".join(bad)
