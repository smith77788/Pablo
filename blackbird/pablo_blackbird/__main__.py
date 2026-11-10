"""CLI обёртки: ``python -m pablo_blackbird --username johndoe``.

Печатает JSON с найденными аккаунтами в stdout. Код выхода 0 — поиск прошёл
(даже если ничего не нашлось), 1 — Blackbird не отработал, 2 — неверные
аргументы.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import BlackbirdError, search


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pablo_blackbird",
        description="Поиск аккаунтов по username или email через Blackbird.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-u", "--username")
    g.add_argument("-e", "--email")
    p.add_argument("--timeout", type=int, default=30, help="таймаут на сайт, с")
    p.add_argument("--max-concurrent-requests", type=int, default=30)
    p.add_argument("--allow-nsfw", action="store_true", help="не исключать NSFW-сайты")
    p.add_argument("--filter", dest="filter_expr", help='например "cat=social"')
    p.add_argument("--proxy")
    p.add_argument("--no-update", action="store_true", help="не обновлять список сайтов")
    p.add_argument("--all", action="store_true", help="выводить и ненайденные сайты")
    args = p.parse_args(argv)

    try:
        result = search(
            username=args.username,
            email=args.email,
            timeout=args.timeout,
            max_concurrent_requests=args.max_concurrent_requests,
            no_nsfw=not args.allow_nsfw,
            filter_expr=args.filter_expr,
            proxy=args.proxy,
            no_update=args.no_update,
        )
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except BlackbirdError as exc:
        print(f"Ошибка Blackbird: {exc}", file=sys.stderr)
        return 1

    out = result.to_dict()
    if not args.all:
        out["accounts"] = result.found
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
