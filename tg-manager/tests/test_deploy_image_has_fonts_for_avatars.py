"""Образ, которым собирается деплой, обязан нести шрифты для аватаров.

`services/avatar_factory.py` рисует аватары аккаунтов и каналов и ищет шрифты по
ЖЁСТКИМ путям (`_FONT_CANDIDATES`). Не найдя ни одного, `_load_font` молча уходит на
`PIL.ImageFont.load_default()`. Это не «шрифт попроще», а другое поведение:

* запасной шрифт растровый и НЕ МАСШТАБИРУЕТСЯ — рисует ~7 пикселей независимо
  от запрошенного размера (а код просит 0.34–0.52 от стороны, то есть 87–133px
  на аватаре 256px). Буква превращается в точку в углу пустого квадрата;
* отдельных кириллических глифов у него нет: «А», «Ж», «Щ» дают одну и ту же
  заглушку 5x7. Владелец русскоязычный, инициалы кириллические.

Ошибки при этом нет ни одной: `_load_font` ловит исключение и продолжает, в
логах тишина. Поэтому проверку держит тест, а не внимательность.

ПОЧЕМУ ИМЕННО КОРНЕВОЙ Dockerfile. Railway собирает тот файл, который назван в
корневом `railway.json` (`dockerfilePath`), и это корневой `Dockerfile` — он
кладёт рядом `tg-manager` и `assistant`. В `tg-manager/Dockerfile` шрифты стояли
давно, но им деплой не собирается, и на продукте это не отражалось.
"""
from __future__ import annotations

import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# каталог шрифтов → пакет Debian, который его приносит
_DIR_TO_PACKAGE = {
    "dejavu": "fonts-dejavu-core",
    "liberation": "fonts-liberation",
    "freefont": "fonts-freefont-ttf",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _deploy_dockerfile() -> str:
    """Путь к Dockerfile, которым РЕАЛЬНО собирается деплой."""
    rj = os.path.join(REPO, "railway.json")
    assert os.path.exists(rj), "в корне нет railway.json — чем собирается деплой?"
    cfg = json.loads(_read(rj))
    rel = (cfg.get("build") or {}).get("dockerfilePath") or "Dockerfile"
    path = os.path.join(REPO, rel)
    assert os.path.exists(path), f"railway.json указывает на {rel}, а файла нет"
    return path


def _required_font_dirs() -> set[str]:
    """Каталоги шрифтов, на которые опирается код (из _FONT_CANDIDATES, не из головы)."""
    from services import avatar_factory

    dirs = set()
    for p in avatar_factory._FONT_CANDIDATES:
        m = re.search(r"/usr/share/fonts/truetype/([^/]+)/", p)
        if m:
            dirs.add(m.group(1))
    assert dirs, "не удалось разобрать _FONT_CANDIDATES — проверка ослепла"
    return dirs


def test_code_still_looks_for_fonts_on_disk():
    """Если код перестанет искать шрифты файлами, этот тест потеряет смысл."""
    from services import avatar_factory

    assert avatar_factory._FONT_CANDIDATES, "_FONT_CANDIDATES опустел"
    assert _required_font_dirs(), "в _FONT_CANDIDATES не осталось каталогов /usr/share/fonts"


def test_deploy_image_installs_at_least_one_font_package():
    dockerfile = _read(_deploy_dockerfile())
    needed = {_DIR_TO_PACKAGE[d] for d in _required_font_dirs() if d in _DIR_TO_PACKAGE}
    assert needed, "ни один каталог из _FONT_CANDIDATES не сопоставлен пакету"
    present = {p for p in needed if p in dockerfile}
    assert present, (
        "образ деплоя не ставит ни одного шрифтового пакета, а avatar_factory ищет "
        f"шрифты в {sorted(_required_font_dirs())}. Без них Pillow уходит на "
        "load_default(): аватар выходит пустым квадратом с 7-пиксельной кляксой, "
        "и кириллица не рисуется вовсе. Добавьте в Dockerfile хотя бы одно из: "
        f"{sorted(needed)}"
    )


def test_fallback_font_really_cannot_do_the_job():
    """Доказательство, а не утверждение: запасной шрифт не тянет нужный размер.

    Если однажды Pillow научит load_default() масштабироваться, этот тест
    покраснеет — и тогда проверку выше можно будет ослабить осознанно.
    """
    from PIL import Image, ImageDraw, ImageFont

    draw = ImageDraw.Draw(Image.new("RGB", (256, 256)))
    box = draw.textbbox((0, 0), "Ж", font=ImageFont.load_default())
    height = box[3] - box[1]
    assert height < 20, (
        f"запасной шрифт нарисовал {height}px — возможно, он теперь масштабируется"
    )


def test_both_dockerfiles_agree_on_fonts():
    """Два образа расходились уже дважды (libc6-dev, шрифты) — держим их вместе."""
    deploy = _read(_deploy_dockerfile())
    inner_path = os.path.join(TG, "Dockerfile")
    if not os.path.exists(inner_path):
        return
    inner = _read(inner_path)
    for package in set(_DIR_TO_PACKAGE.values()):
        assert (package in deploy) == (package in inner), (
            f"{package} стоит только в одном из двух Dockerfile — "
            "расхождение уже приводило к тому, что фикс уезжал в файл, "
            "которым деплой не собирается"
        )
