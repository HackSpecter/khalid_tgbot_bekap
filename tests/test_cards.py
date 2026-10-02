from zoneinfo import ZoneInfo

from tgbackup.cards import deleted_card, edited_card, quote

TZ = ZoneInfo("Asia/Dushanbe")


def row(**overrides):
    base = {
        "chat_id": -100, "sender_id": 7, "sender_name": "Shahrom <гений>", "sender_username": "Shahrom_R",
        "outgoing": 0, "text": "тест", "date": "2026-10-02T09:58:01+00:00", "edited": 0, "edit_date": None,
        "media_type": None, "media_note": None, "service_action": None, "fwd_from_name": None, "fwd_from_id": None,
        "read_at": None,
    }
    base.update(overrides)
    return base


GROUP = {"type": "group", "title": "Darvoz сила 💪", "username": None}


def test_deleted_card_group():
    text = deleted_card(row(), GROUP, "2026-10-02T09:58:13+00:00", TZ)
    assert '<a href="tg://user?id=7">Shahrom &lt;гений&gt;</a> · @Shahrom_R' in text
    assert "💬 <b>Darvoz сила 💪</b>" in text
    assert "<blockquote>тест</blockquote>" in text
    assert "14:58:01" in text and "удалено" in text and "14:58:13" in text


def test_deleted_card_media_without_text_has_no_empty_quote():
    text = deleted_card(row(text=None, media_type="voice"), None, "2026-10-02T09:58:13+00:00", TZ)
    assert "🎤 Голосовое" in text and "blockquote" not in text


def test_long_text_is_collapsed_and_truncated():
    assert quote("x" * 400).startswith("<blockquote expandable>")
    assert len(quote("y" * 10_000)) < 3000


def test_edited_card():
    text = edited_card(row(text="было <b>"), "стало", "2026-10-02T10:00:00+00:00", None, GROUP, TZ)
    assert "<blockquote>было &lt;b&gt;</blockquote>" in text and "<blockquote>стало</blockquote>" in text
