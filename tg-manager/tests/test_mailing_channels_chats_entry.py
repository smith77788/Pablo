"""Рассылка по каналам и по чатам видна на экране «Рассылки» под своим именем.

Владелец не нашёл рассылку по каналам/чатам: пост в каналы прятался за кнопкой
«📤 Публикация», а пост в чаты — за «🚀 Всё сразу» и галочкой внутри. Теперь
у обоих способов своя кнопка, а «💬 В чаты» открывает кампанию с уже
отмеченными чатами.
"""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _screen(sid: str) -> str:
    s = HTML.index(f'<div class="screen" id="{sid}">')
    e = HTML.find('<div class="screen" id="', s + 10)
    return HTML[s:e]


def test_broadcasts_hub_names_channels_and_chats():
    hub = _screen("s-broadcasts")
    assert re.search(r'onclick="openMassPub\(\)"[^>]*>📡 В каналы<', hub)
    assert re.search(r'''onclick="openUniCampaign\('chats'\)"[^>]*>💬 В чаты<''', hub)


def test_mass_publish_lists_chats_as_recipient():
    mp = _screen("s-masspub")
    assert "onclick=\"openUniCampaign('chats')\"" in mp
    assert "Чаты и группы" in mp


def test_uni_campaign_preselects_requested_kind():
    i = HTML.index("function openUniCampaign(kind)")
    body = HTML[i:HTML.index("\n}\n", i)]
    m = re.search(r"const UC_KIND_BOX = \{([^}]*)\}", HTML)
    assert m and "chats:'ucKChats'" in m.group(1)
    # Галочку ставим ПОСЛЕ общего сброса всех четырёх, иначе сброс её снимет.
    assert body.index(".checked = false") < body.index("UC_KIND_BOX[kind]")
    assert "ucKindToggle()" in body[body.index("UC_KIND_BOX[kind]"):]
