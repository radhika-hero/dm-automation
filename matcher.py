"""Keyword matching + which posts are in scope.

Scope (Pritam's decision, 2026-08-29): **only posts whose caption carries the CTA.** A post
is in scope when its caption mentions one of the configured keywords — i.e. the caption
literally says "comment BITES and I'll send it". That keeps the poller off old posts nobody
was invited to comment a keyword on, and needs no hand-maintained media-id list.

`config.media_ids`, when non-empty, overrides the caption scan and pins an exact list.

Matching a comment is deliberately word-boundary based, not substring: "bites" must be a word
so "bitesize" or a sentence mentioning the product in passing does not fire a DM.
"""
from __future__ import annotations

import re
import unicodedata


def _normalise(text: str) -> str:
    """Lowercase, strip accents, collapse punctuation to spaces — so '@dietswad BITES!!' matches."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def matches(text: str, keyword: str, mode: str = "word") -> bool:
    haystack = _normalise(text)
    needle = _normalise(keyword)
    if not needle:
        return False
    if mode == "contains":
        return needle in haystack
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def match_comment(comment_text: str, keywords: list[dict],
                  media_id: str | None = None) -> dict | None:
    """First keyword (config order) whose trigger appears in the comment. None = no match.

    An entry may carry its own `media_ids` — then it only answers comments on THOSE posts.
    That is how one spoken keyword can serve two different reels: LABEL is the CTA in both
    SCR-04 and SCR-07 (audio wins, Pritam 2026-08-08) but each reel has its own blog page and
    its own PDF, so each gets an entry scoped to its own media ids.
    """
    for entry in keywords:
        if not entry.get("enabled", True):
            continue
        scope = entry.get("media_ids") or []
        if scope and (media_id is None or media_id not in scope):
            continue
        for trigger in [entry["keyword"], *entry.get("aliases", [])]:
            if matches(comment_text, trigger, entry.get("match", "word")):
                return entry
    return None


def caption_has_cta(caption: str, keywords: list[dict]) -> bool:
    """Is this post inviting a keyword comment? Uses 'contains' — captions are prose."""
    for entry in keywords:
        if not entry.get("enabled", True):
            continue
        for trigger in [entry["keyword"], *entry.get("aliases", [])]:
            if matches(caption or "", trigger, "contains"):
                return True
    return False


def select_media(all_media: list[dict], keywords: list[dict]) -> list[dict]:
    return [m for m in all_media if caption_has_cta(m.get("caption", ""), keywords)]
