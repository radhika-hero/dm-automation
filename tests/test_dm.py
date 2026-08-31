"""Tests for the parts that can be wrong without an API call.

Nothing here touches the network. The one-shot send is deliberately NOT unit-tested against
Meta — it is tested live, once, with --limit 1.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import MAX_ATTEMPTS, Ledger  # noqa: E402
from matcher import caption_has_cta, match_comment, matches, select_media  # noqa: E402
from meta import build_card  # noqa: E402

KEYWORDS = [
    {"keyword": "BITES", "url": "https://x/1", "title": "T", "button_title": "B"},
    {"keyword": "FUEL", "aliases": ["PREWORKOUT"], "url": "https://x/2",
     "title": "T2", "button_title": "B2"},
    {"keyword": "OFF", "enabled": False, "url": "https://x/3", "title": "T3", "button_title": "B3"},
]


# ---------- matching ----------

@pytest.mark.parametrize("text", ["BITES", "bites", "@dietswad BITES!!", "yes bites please"])
def test_matches_the_word(text):
    assert match_comment(text, KEYWORDS)["keyword"] == "BITES"


@pytest.mark.parametrize("text", ["bitesize", "rabbits", "biting", ""])
def test_word_boundary_prevents_false_fires(text):
    assert match_comment(text, KEYWORDS) is None


def test_alias_triggers_its_entry():
    assert match_comment("preworkout", KEYWORDS)["keyword"] == "FUEL"


def test_disabled_entry_never_fires():
    assert match_comment("OFF", KEYWORDS) is None


def test_contains_mode_is_opt_in():
    assert matches("bitesize", "BITES", "contains")
    assert not matches("bitesize", "BITES", "word")


# ---------- scope ----------

def test_only_cta_captions_are_in_scope():
    media = [
        {"id": "1", "caption": "Comment BITES and I'll send it to you"},
        {"id": "2", "caption": "Happy Rakhi from all of us"},
        {"id": "3", "caption": None},
    ]
    assert [m["id"] for m in select_media(media, KEYWORDS)] == ["1"]


def test_caption_scope_uses_contains_not_word():
    # A caption may glue the keyword to punctuation or hashtags.
    assert caption_has_cta("…comment 'BITES'👇 #healthysnacks", KEYWORDS)


# ---------- the card ----------

def test_card_shape_matches_the_proven_payload():
    card = build_card({"title": "T", "subtitle": "S", "url": "https://x", "button_title": "Go"})
    payload = card["attachment"]["payload"]
    assert payload["template_type"] == "generic"
    element = payload["elements"][0]
    assert element["buttons"] == [{"type": "web_url", "url": "https://x", "title": "Go"}]
    assert "image_url" not in element  # optional, and untested against the live render


def test_card_includes_image_when_given():
    card = build_card({"title": "T", "url": "https://x", "button_title": "Go",
                       "image_url": "https://img"})
    assert card["attachment"]["payload"]["elements"][0]["image_url"] == "https://img"


# ---------- the ledger ----------

def test_terminal_states_are_never_retried(tmp_path):
    for status in ("sent", "already", "expired"):
        ledger = Ledger(tmp_path / f"{status}.json")
        ledger.record("c1", status)
        assert ledger.is_done("c1")


def test_failed_is_retried_until_the_attempt_cap(tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    for _ in range(MAX_ATTEMPTS - 1):
        ledger.record("c1", "failed", error="boom")
        assert not ledger.is_done("c1")
    ledger.record("c1", "failed", error="boom")
    assert ledger.is_done("c1")


def test_every_write_is_flushed_to_disk(tmp_path):
    path = tmp_path / "l.json"
    Ledger(path).record("c1", "sent", message_id="m1")
    assert json.loads(path.read_text(encoding="utf-8"))["c1"]["message_id"] == "m1"
    assert Ledger(path).is_done("c1")  # survives a reload — this is the whole point


def test_unknown_comment_is_not_done(tmp_path):
    assert not Ledger(tmp_path / "l.json").is_done("nope")


# ---------- the shipped config ----------

def test_shipped_config_passes_its_own_validator():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import validate
    assert validate.main() == 0


# ---------- shared trigger, two reels (LABEL: SCR-04 and SCR-07) ----------

SPLIT = [
    {"keyword": "LABEL", "url": "https://x/a", "title": "A", "button_title": "B",
     "media_ids": ["m4"]},
    {"keyword": "LABEL", "url": "https://x/b", "title": "B", "button_title": "B",
     "media_ids": ["m7"]},
]


def test_scoped_entry_answers_only_its_own_post():
    assert match_comment("LABEL", SPLIT, media_id="m4")["url"] == "https://x/a"
    assert match_comment("LABEL", SPLIT, media_id="m7")["url"] == "https://x/b"


def test_scoped_entry_ignores_a_post_it_does_not_own():
    assert match_comment("LABEL", SPLIT, media_id="somewhere-else") is None


def test_unscoped_entry_still_answers_any_post():
    plain = [{"keyword": "BITES", "url": "https://x/1", "title": "T", "button_title": "B"}]
    assert match_comment("bites", plain, media_id="anything")["keyword"] == "BITES"


def test_validator_rejects_a_shared_trigger_with_no_media_scope(tmp_path, monkeypatch):
    import validate
    bad = {"keywords": [
        {"keyword": "LABEL", "enabled": True, "url": "https://x/a", "title": "A",
         "button_title": "B"},
        {"keyword": "LABEL", "enabled": True, "url": "https://x/b", "title": "B",
         "button_title": "B"},
    ]}
    path = tmp_path / "keywords.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr(validate, "CONFIG_PATH", path)
    assert validate.main() == 1


# --- Facebook (added 2026-09-01, once the token carried pages_messaging) --------------------

TWO_PLATFORM = [
    {"keyword": "COOKIE", "url": "https://x/biscuit", "title": "T", "button_title": "B",
     "media_ids": ["ig1"], "facebook_post_ids": ["page_1"]},
    {"keyword": "CHEMICAL", "url": "https://x/snacks", "title": "T", "button_title": "B",
     "media_ids": ["ig1"], "facebook_post_ids": ["page_1"]},
]


def test_facebook_and_instagram_pins_are_separate_namespaces():
    """An Instagram media id must never satisfy a Facebook scope, or vice versa."""
    assert match_comment("COOKIE", TWO_PLATFORM, media_id="ig1",
                         platform="instagram")["url"] == "https://x/biscuit"
    assert match_comment("COOKIE", TWO_PLATFORM, media_id="page_1",
                         platform="facebook")["url"] == "https://x/biscuit"
    # The right id on the WRONG platform must not match.
    assert match_comment("COOKIE", TWO_PLATFORM, media_id="ig1", platform="facebook") is None
    assert match_comment("COOKIE", TWO_PLATFORM, media_id="page_1", platform="instagram") is None


def test_one_post_can_answer_two_different_words():
    """COOKIE and CHEMICAL both live on the shooting videos, with different guides."""
    for platform, pid in (("instagram", "ig1"), ("facebook", "page_1")):
        assert match_comment("COOKIE", TWO_PLATFORM, media_id=pid,
                             platform=platform)["url"] == "https://x/biscuit"
        assert match_comment("CHEMICAL", TWO_PLATFORM, media_id=pid,
                             platform=platform)["url"] == "https://x/snacks"


def test_unpinned_entry_never_answers_on_facebook():
    """On Instagram an unpinned entry answers anywhere (legacy). Facebook must NOT inherit that.

    The live bug on 2026-08-31: SWEET was enabled and unpinned, so "so sweet" on a
    label-reading reel returned the sweetener card. A new platform starts strict.
    """
    unpinned = [{"keyword": "SWEET", "url": "https://x/s", "title": "T", "button_title": "B"}]
    assert match_comment("so sweet", unpinned, media_id="anything",
                         platform="instagram") is not None
    assert match_comment("so sweet", unpinned, media_id="anything",
                         platform="facebook") is None
