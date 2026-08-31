"""Dump recent IG posts with their media ids, so `media_ids` can be filled in keywords.json.

Pritam, 2026-08-31: pin every keyword to its own posts rather than relying on the caption
scan. Two reasons it is worth the effort:

  * It is the ONLY way two entries can share one spoken trigger. LABEL is the CTA in both
    SCR-04 and SCR-07, and validate.py fails closed unless each enabled entry is pinned.
  * It removes a real false-positive risk on short, common words. "DIET" is the worst case:
    `caption_has_cta` matches captions with `contains`, and almost every caption contains
    "Diet Swad", so an unpinned DIET entry pulls nearly every post into scope. `match_comment`
    then word-matches "diet", so a comment reading "love diet swad" would fire a DM.

    python -X utf8 list_media.py                # table, newest first
    python -X utf8 list_media.py --limit 50
    python -X utf8 list_media.py --json         # {trigger: [media_id, ...]} to paste in

Needs the same environment as run.py (META_SYSTEM_USER_TOKEN, IG_USER_ID, FB_PAGE_ID). It only
READS: no comment is answered and no DM is sent, so it is safe to run any time.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from matcher import matches
from meta import GraphError, MetaClient

CONFIG_PATH = Path(__file__).with_name("keywords.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25, help="how many recent posts to fetch")
    ap.add_argument("--json", action="store_true",
                    help="emit {trigger: [media_id, ...]} instead of the table")
    args = ap.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keywords = config.get("keywords", [])

    client = MetaClient()
    missing = client.missing_env()
    if missing:
        print(f"ERROR: missing env: {', '.join(missing)}")
        return 1

    try:
        media = client.recent_media(args.limit)
    except GraphError as exc:
        print(f"ERROR: could not read media: {exc}")
        return 1

    # Which triggers does each caption invite? Deliberately reports EVERY match rather than
    # the first, because seeing that a caption matches three triggers is the whole point.
    rows = []
    for post in media:
        caption = post.get("caption", "") or ""
        hits = []
        for entry in keywords:
            for trigger in [entry["keyword"], *entry.get("aliases", [])]:
                if matches(caption, trigger, "contains") and trigger not in hits:
                    hits.append(trigger)
        rows.append((post.get("id", "?"), post.get("timestamp", "")[:10],
                     " ".join(caption.split())[:58], hits))

    if args.json:
        by_trigger: dict[str, list[str]] = {}
        for media_id, _, _, hits in rows:
            for trigger in hits:
                by_trigger.setdefault(trigger, []).append(media_id)
        print(json.dumps(by_trigger, indent=2))
        return 0

    print(f"{'media_id':<20}{'date':<12}{'caption':<60}triggers in caption")
    print("-" * 110)
    for media_id, date, caption, hits in rows:
        flag = "  <-- AMBIGUOUS" if len(hits) > 1 else ""
        print(f"{media_id:<20}{date:<12}{caption:<60}{', '.join(hits) or '-'}{flag}")

    ambiguous = sum(1 for *_, hits in rows if len(hits) > 1)
    print(f"\n{len(rows)} posts. {ambiguous} caption(s) match more than one trigger — those are "
          f"exactly the posts that need media_ids set, or the first matching entry answers them all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
