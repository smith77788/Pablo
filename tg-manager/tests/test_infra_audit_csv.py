"""Журнал инфраструктуры: показанные записи выгружаются в CSV с русскими
названиями действий и пулов, причиной по-русски и исходной ошибкой."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/infra.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_csv_rows_are_russian_and_escaped():
    js = ("const auditRu=a=>'ru:'+a;const infraPoolLabel=p=>({cooldown:'На остывании'})[p]||p;"
          + _fn("iaCsv")
          + "console.log(JSON.stringify(iaCsv([{occurred_at:'2026-11-22T10:00:00Z',action:'join',result:'fail',"
            "account_label:'+7999',account_pool:'cooldown',target:'@chat',error_ru:'Флуд \"30 с\"',"
            "error_raw:'FLOOD_WAIT_30\\nx',flood_wait_s:30,operation_id:12}])))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = json.loads(out.stdout).split("\r\n")
    assert lines[0].startswith('"Когда";"Действие";"Итог"')
    assert lines[1] == ('"2026-11-22T10:00:00Z";"ru:join";"fail";"+7999";"На остывании";"@chat";'
                        '"Флуд ""30 с""";"FLOOD_WAIT_30 x";"30";"12"')


def test_export_button_is_on_the_list():
    assert 'onclick="iaExportCsv()"' in _fn("_iaRender")
