"""Шаг «Сообщение» с фото, видео или файлом: проверка настроек."""
import pytest

from services import workflow_engine as wf


def test_media_makes_text_optional_and_keeps_kind():
    s = wf.validate_step({"type": "message", "media": "https://ex.com/a.mp4",
                          "media_kind": "video"})
    assert s["media"] == "https://ex.com/a.mp4" and s["media_kind"] == "video"
    assert s["text"] == ""


def test_media_defaults_to_photo_and_accepts_file_id():
    s = wf.validate_step({"type": "message", "text": "Привет",
                          "media": "AgACAgIAAxkBAAIBc2Zfile_id_example"})
    assert s["media_kind"] == "photo"


def test_text_still_required_without_media():
    with pytest.raises(ValueError):
        wf.validate_step({"type": "message", "text": ""})


@pytest.mark.parametrize("media", ["http://ex.com/a.jpg", "javascript:alert(1)",
                                   "short", "https://" + "a" * 600])
def test_bad_media_is_rejected(media):
    with pytest.raises(ValueError):
        wf.validate_step({"type": "message", "text": "x", "media": media})


def test_unknown_kind_and_long_caption_are_rejected():
    with pytest.raises(ValueError):
        wf.validate_step({"type": "message", "text": "x",
                          "media": "https://ex.com/a.jpg", "media_kind": "audio"})
    with pytest.raises(ValueError, match="1024"):
        wf.validate_step({"type": "message", "text": "я" * 1100,
                          "media": "https://ex.com/a.jpg"})
