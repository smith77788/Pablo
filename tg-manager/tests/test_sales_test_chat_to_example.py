"""Тест-чат ИИ-менеджера: ответ можно сохранить в примеры диалога (с правкой),
а диалог — начать заново."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/sales_manager.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_pair_takes_nearest_client_message():
    js = _fn("spChatPair") + (
        "const h=[{role:'user',content:'цена?'},{role:'assistant',content:'100'},"
        "{role:'user',content:'а доставка?'},{role:'assistant',content:'бесплатно'},{role:'assistant',content:'ещё'}];"
        "console.log(JSON.stringify([spChatPair(h,1),spChatPair(h,4),spChatPair(h,0),spChatPair([{role:'assistant',content:'x'}],0)]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        {"user_msg": "цена?", "assistant_msg": "100"},
        {"user_msg": "а доставка?", "assistant_msg": "ещё"},
        None, None]


def test_save_posts_edited_answer_to_examples():
    save = _fn("spChatSaveExample")
    assert "'/api/miniapp/sales/persona/' + _spChatPid + '/example'" in save
    assert "assistant_msg: a" in save and "spChatExA" in save
    assert "spChatToExample(" in _fn("_spChatRender")
    assert "spChatReset()" in _fn("spChat")
