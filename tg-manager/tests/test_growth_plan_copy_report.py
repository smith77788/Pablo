"""План роста: «📋 Отчёт» копирует цели текстом, отстающие — первыми."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/growth_plan.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_report_puts_lagging_first():
    goals = [
        {"name": "Канал А", "asset_kind": "channel", "status": "active", "state_ru": "успеваете",
         "deadline_ru": "1 дек", "progress": {"state": "on_track", "current": 1200, "target": 2000, "pct": 60, "need_per_day": 30}},
        {"name": "Бот Б", "asset_kind": "bot", "status": "active", "state_ru": "отстаёт",
         "deadline_ru": "5 дек", "progress": {"state": "behind", "current": 10, "target": 500, "pct": 2, "need_per_day": 40}},
    ]
    js = ("const _gpNum = n => Number(n || 0).toLocaleString('ru-RU');" + _fn("gpReportText") +
          "console.log(JSON.stringify(gpReportText(" + json.dumps(goals, ensure_ascii=False) + ")))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = json.loads(out.stdout).split("\n")
    assert lines[0] == "План роста: целей 2"
    assert lines[1].startswith("• 🤖 Бот Б — отстаёт: 10 из 500 (2%)") and "нужно 40 в день" in lines[1]
    assert "Канал А" in lines[2]


def test_summary_has_copy_button():
    assert "gpCopyReport()" in _fn("_gpSummary")
    assert "copyToClipboard(gpReportText(goals)" in _fn("gpCopyReport")
