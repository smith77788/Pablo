"""Тесты обёртки без сети: вместо настоящего Blackbird — поддельный upstream."""

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pablo_blackbird as bb

FAKE = textwrap.dedent(
    """
    import json, os, sys
    args = sys.argv[1:]
    kind = "username" if "--username" in args else "email"
    q = args[args.index("--" + kind) + 1]
    if q == "boom":
        print("traceback", file=sys.stderr); sys.exit(3)
    open("argv.txt", "w").write(json.dumps(args))
    if q == "nobody":
        sys.exit(0)
    d = os.path.join("results", q + "_blackbird"); os.makedirs(d)
    with open(os.path.join(d, q + "_blackbird.json"), "w") as f:
        json.dump([{"name": "GitHub", "url": "https://github.com/" + q,
                    "category": "coding", "status": "FOUND", "metadata": None}], f)
    """
)


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    up = tmp_path / "upstream"
    (up / "data").mkdir(parents=True)
    (up / "blackbird.py").write_text(FAKE)
    monkeypatch.setenv("BLACKBIRD_WORKDIR", str(tmp_path / "work"))
    return up


def test_username_search_parses_json(upstream, tmp_path):
    r = bb.search(username="johndoe", upstream_dir=upstream)
    assert r.kind == "username"
    assert [a["name"] for a in r.found] == ["GitHub"]
    argv = json.loads((tmp_path / "work" / "app" / "argv.txt").read_text())
    assert argv[:3] == ["--username", "johndoe", "--json"]
    assert "--no-nsfw" in argv
    # результаты не копятся между запусками
    assert not (tmp_path / "work" / "app" / "results").exists()


def test_email_and_nothing_found(upstream):
    r = bb.search(email="nobody", upstream_dir=upstream)
    assert r.kind == "email" and r.accounts == []
    assert r.to_dict()["found_count"] == 0


def test_failure_raises(upstream):
    with pytest.raises(bb.BlackbirdError, match="кодом 3"):
        bb.search(username="boom", upstream_dir=upstream)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"username": "a", "email": "b"}, {"username": "--about"},
     {"username": "../etc"}, {"username": "  "}],
)
def test_bad_arguments(upstream, kwargs):
    with pytest.raises(ValueError):
        bb.search(upstream_dir=upstream, **kwargs)


def test_downloaded_site_list_survives_upstream_update(upstream, tmp_path):
    bb.search(username="johndoe", upstream_dir=upstream)
    cached = tmp_path / "work" / "app" / "data" / "wmn-data.json"
    cached.write_text("{}")
    (upstream / "blackbird.py").write_text(FAKE + "\n# new version\n")
    bb.search(username="johndoe", upstream_dir=upstream)
    assert cached.read_text() == "{}"


def test_cli_prints_found(upstream, monkeypatch, capsys):
    monkeypatch.setattr(bb, "UPSTREAM_DIR", upstream)
    from pablo_blackbird.__main__ import main

    assert main(["-u", "johndoe"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["found_count"] == 1 and out["query"] == "johndoe"
