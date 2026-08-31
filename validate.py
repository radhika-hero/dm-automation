"""Config gate — runs before the poller, in the same spirit as the scheduler's validate_schedule.py.

The DM is one-shot and irreversible, so a malformed entry is not a thing to discover at send
time. FAIL-CLOSED, like the scheduler's validator (locked by Pritam 2026-08-02): one bad entry
stops the whole run.

    python -X utf8 validate.py     # exit 0 = safe

Catches: an enabled entry with no/invalid url, a missing title/button_title, Instagram's
character limits on the card, duplicate keywords, >3 buttons' worth of config, Markdown
(`**bold**` publishes as visible asterisks — the scheduler shipped that live on 2026-08-20),
and a link that would ship without working UTM tracking (see UTM_KEYS below).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = Path(__file__).with_name("keywords.json")

# Generic-template limits (Messenger/IG platform docs).
MAX_TITLE = 80
MAX_SUBTITLE = 80
MAX_BUTTON_TITLE = 20

MARKDOWN = [
    (r"\*\*", "bold **asterisks** — publishes as literal asterisks"),
    (r"(?m)^#{1,6}\s", "# heading"),
    (r"(?m)^>\s", "> quote"),
    (r"\[[^\]]+\]\([^)]+\)", "[text](url) link"),
    (r"`", "backtick"),
]


# --- UTM tracking (added 2026-08-31) -------------------------------------------------------
# EXACTLY three parameters, because dietswad.in's analytics is configured for three
# (Pritam, 2026-08-31). utm_content / utm_term are NOT used — the content identifier lives
# INSIDE utm_campaign, e.g. the two LABEL entries are `label-04` and `label-07`.
#
# Why this is a hard gate and not a nicety: the DM button is the ONE Instagram surface where a
# link is genuinely tappable (captions and comments never linkify), so it is the only place the
# funnel can be measured at all. A link that ships without UTMs is invisible in GA4 and, worse,
# looks fine — you only discover it when a campaign reports zero and you cannot tell whether
# nobody clicked or nothing was tagged.
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")
# Lowercase kebab-case. GA4 treats `Label-04` and `label-04` as different campaigns, which
# silently splits one funnel into two rows in every report.
CAMPAIGN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def check_utm(url: str, errors: list[str], where: str) -> str | None:
    """Validate the three UTMs on one link. Returns its campaign, for the uniqueness check."""
    query = parse_qs(urlparse(url).query)
    for key in UTM_KEYS:
        if not query.get(key, [""])[0]:
            errors.append(f"{where}: 'url' is missing {key} — the DM click would be untracked")
    extra = sorted(k for k in query if k.startswith("utm_") and k not in UTM_KEYS)
    if extra:
        errors.append(f"{where}: 'url' carries {', '.join(extra)} — the site is configured for "
                      f"exactly {len(UTM_KEYS)} UTMs; put the content id inside utm_campaign")
    campaign = query.get("utm_campaign", [""])[0]
    if campaign and not CAMPAIGN_RE.match(campaign):
        errors.append(f"{where}: utm_campaign {campaign!r} must be lowercase kebab-case")
    return campaign or None


def check_text(field: str, value: str, limit: int, errors: list[str], where: str) -> None:
    if not value:
        errors.append(f"{where}: '{field}' is empty")
        return
    if len(value) > limit:
        errors.append(f"{where}: '{field}' is {len(value)} chars, limit {limit}")
    for pattern, label in MARKDOWN:
        if re.search(pattern, value):
            errors.append(f"{where}: '{field}' contains Markdown ({label}) — card text is plain text")


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []

    public = config.get("public_reply", {})
    if public.get("enabled"):
        check_text("public_reply.text", public.get("text", ""), 2200, errors, "public_reply")

    seen: dict[str, int] = {}
    campaigns: dict[str, int] = {}
    enabled = 0
    for index, entry in enumerate(config.get("keywords", [])):
        where = f"keywords[{index}] '{entry.get('keyword', '?')}'"

        keyword = (entry.get("keyword") or "").strip()
        if not keyword:
            errors.append(f"{where}: missing 'keyword'")
            continue
        # A trigger may legitimately appear twice — one spoken word, two reels, two landing
        # pages (LABEL: SCR-04 and SCR-07). That is only safe when each entry is pinned to its
        # own posts via `media_ids`; otherwise the first entry in file order would swallow
        # every comment and the second flow would silently never fire.
        for trigger in [keyword, *entry.get("aliases", [])]:
            key = trigger.lower()
            if key in seen:
                first = config["keywords"][seen[key]]
                unscoped = [
                    e for e in (first, entry)
                    if e.get("enabled", True) and not (e.get("media_ids") or [])
                ]
                if unscoped:
                    errors.append(
                        f"{where}: trigger '{trigger}' is also used by keywords[{seen[key]}]. "
                        "A shared trigger needs 'media_ids' on every enabled entry that uses "
                        "it, or only the first one would ever fire.")
                # Pinned is not enough: the pins must not OVERLAP. Two entries sharing a trigger
                # AND a post is the same bug wearing a disguise — match_comment returns the first
                # entry in file order, so the second silently never fires. Found 2026-08-31, when
                # both LABEL entries pinned to the same three posts because both reels say the
                # word out loud and the caption cannot tell them apart.
                elif all(e.get("enabled", True) for e in (first, entry)):
                    shared = set(first.get("media_ids") or []) & set(entry.get("media_ids") or [])
                    if shared:
                        errors.append(
                            f"{where}: trigger '{trigger}' is shared with keywords[{seen[key]}] "
                            f"and BOTH are pinned to the same post(s): {sorted(shared)}. "
                            "Only the first would ever fire. Split the media_ids so each entry "
                            "owns its own posts.")
            seen[key] = index

        if entry.get("match", "word") not in ("word", "contains"):
            errors.append(f"{where}: 'match' must be 'word' or 'contains'")

        # --- schema v2 identity ---------------------------------------------------------
        # `id` is the entry's STABLE identity and `keyword` is merely the spoken word. Two
        # entries may legitimately share a keyword (LABEL) but never an id. Checked across
        # EVERY entry, disabled ones included, because an id is permanent rather than runtime
        # state — a duplicate must be caught before the second entry is ever switched on.
        entry_id = (entry.get("id") or "").strip()
        if not entry_id:
            errors.append(f"{where}: missing 'id' — every entry needs a stable id (schema v2)")
        elif not CAMPAIGN_RE.match(entry_id):
            errors.append(f"{where}: id {entry_id!r} must be lowercase kebab-case")
        elif entry_id in campaigns:
            errors.append(f"{where}: id {entry_id!r} is already used by "
                          f"keywords[{campaigns[entry_id]}] — ids must be unique")
        else:
            campaigns[entry_id] = index

        # id == utm_campaign is what lets a GA4 row be traced back to exactly one entry. If they
        # drift, a campaign's clicks can no longer be attributed to the DM that produced them.
        url_now = entry.get("url", "")
        if url_now and entry_id:
            camp = parse_qs(urlparse(url_now).query).get("utm_campaign", [""])[0]
            if camp and camp != entry_id:
                errors.append(f"{where}: utm_campaign {camp!r} does not match id {entry_id!r} — "
                              "they must be identical, or GA4 cannot be traced back to an entry")

        if not entry.get("enabled", True):
            continue
        enabled += 1

        url = entry.get("url", "")
        if not url:
            errors.append(f"{where}: enabled but 'url' is empty — it would DM a broken card")
        elif not url.startswith("https://"):
            errors.append(f"{where}: 'url' must be https (got {url!r})")
        else:
            check_utm(url, errors, where)

        check_text("title", entry.get("title", ""), MAX_TITLE, errors, where)
        check_text("button_title", entry.get("button_title", ""), MAX_BUTTON_TITLE, errors, where)
        if entry.get("subtitle"):
            check_text("subtitle", entry["subtitle"], MAX_SUBTITLE, errors, where)
        if entry.get("image_url") and not entry["image_url"].startswith("https://"):
            errors.append(f"{where}: 'image_url' must be https and publicly fetchable")

    if errors:
        print(f"❌ {len(errors)} problem(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"✅ config valid — {enabled} keyword(s) enabled, {len(seen)} trigger(s) defined")
    if enabled == 0:
        print("   (nothing enabled: the poller will read comments and send nothing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
