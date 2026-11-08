"""Пустой список ведёт к первому действию, а не просто говорит «пусто».

У лидеров на каждом пустом списке есть одна кнопка первого действия. Здесь —
у списков, где подсказка просила «нажмите + Создать» или «добавьте…», а кнопки
не было: кнопка есть, и функция, которую она зовёт, существует.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

_EXPECT = {
    "loadCompetitors": "openCompModal()", "loadCRM": "openCrmContactModal()",
    "renderBcScheduleList": "openBcScheduleModal()", "openAiMemory": "openAiMemoryModal()",
    "openNodes": "openNodeModal()", "openDmCampaigns": "openCmpModal()",
    "openWarmup": "openWarmupModal()", "openFolders": "openFolderCompose()",
    "openApiKeys": "openApiKeyModal()", "loadParserRuns": "openParserModal()",
    "loadAssetTpl": "openAssetTplModal()", "openPresencePacks": "openPresencePackModal()",
    "openGlobalPresence": "openGpCreateModal()", "openNarrative": "openNarrativeModal()",
    "loadNetworkBuilder": "openNetBuilderCreateModal()",
    "loadWorkflows": "openWorkflowCreateModal()", "openWorkflowDetail": "openWfAddStepModal()",
    "openCommunity": "openCommunityCreate()", "openRegCheck": "openRegCheckModal()",
}


def _body(src: str, fn: str) -> str:
    m = re.search(r"(?:async )?function " + fn + r"\(", src)
    assert m, fn
    nxt = re.search(r"\n(?:async )?function \w+\(", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else 8000)]


def test_empty_lists_offer_the_first_action():
    src = miniapp_html()
    for fn, call in _EXPECT.items():
        body = _body(src, fn)
        assert f"fn:'{call}'" in body, f"{fn}: у пустого списка нет кнопки {call}"
        name = call.split("(")[0]
        assert re.search(r"function " + name + r"\(", src), f"{name} не существует"


def test_cta_share_does_not_shrink():
    src = miniapp_html()
    calls = re.findall(r"\bempty\((.{0,400}?)\)\s*[;)]", src)
    with_cta = sum(1 for c in calls if "label:" in c)
    assert with_cta >= 60, with_cta   # грубый счёт по регулярке; был 47 до этой правки
