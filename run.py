"""Comment → DM poller. One pass: read comments, match keywords, send the card, record it.

    python -X utf8 run.py --dry-run     # read + match + print, send NOTHING
    python -X utf8 run.py               # live
    python -X utf8 run.py --limit 1     # live, but stop after one send (use for the first test)

Instagram only for v1 — Facebook Messenger needs `pages_messaging` / App Review (plan §2a).
The sender takes `platform` as a parameter so adding FB later is config, not a rewrite.

Exit codes: 0 = clean, 1 = at least one terminal failure (so the Actions run goes RED).
The scheduler learned on 2026-08-02 that a green tick can hide "posted 0" — not repeating that.

⚠️ The DM is ONE-SHOT AND IRREVERSIBLE (plan §3.1). Test with --dry-run first, always.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledger import Ledger
from matcher import match_comment, select_media
from meta import GraphError, MetaClient, build_card

CONFIG_PATH = Path(__file__).with_name("keywords.json")

# Meta's private-reply window (plan §3.2). Reply after this and the call fails, so we mark
# the comment `expired` and stop looking at it. Kept slightly under 7 days so a comment does
# not expire *between* the check and the send.
PRIVATE_REPLY_WINDOW = timedelta(days=6, hours=23)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def log(line: str) -> None:
    print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="read and match, but send nothing and write nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N sends (0 = no limit)")
    args = ap.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keywords = config.get("keywords", [])
    if not keywords:
        log("no keywords configured — nothing to do")
        return 0

    client = MetaClient()
    missing = client.missing_env()
    if missing:
        log(f"ERROR: missing env: {', '.join(missing)}")
        return 1

    ledger = Ledger()
    own = (config.get("own_username") or "dietswad").lower()
    public_reply = config.get("public_reply", {})

    # ---- pick the posts in scope ----
    pinned = config.get("media_ids") or []
    if pinned:
        media = client.media_by_ids(pinned)
        log(f"scope: {len(media)} pinned media id(s)")
    else:
        recent = client.recent_media(config.get("scan_media_limit", 25))
        media = select_media(recent, keywords)
        log(f"scope: {len(media)} of {len(recent)} recent posts carry a keyword CTA")

    sent = 0
    failures = 0
    now = datetime.now(timezone.utc)

    for post in media:
        if not post.get("comments_count"):
            continue
        try:
            comments = client.comments(post["id"])
        except GraphError as exc:
            log(f"FAIL  media {post['id']}: could not read comments: {exc}")
            failures += 1
            continue

        for comment in comments:
            comment_id = comment["id"]
            username = (comment.get("username") or "").lower()

            # Our own hashtag first-comment posted by the scheduler — never a lead (plan §2).
            if username == own:
                continue
            if ledger.is_done(comment_id):
                continue

            entry = match_comment(comment.get("text", ""), keywords, media_id=post["id"])
            if not entry:
                continue

            created = parse_ts(comment.get("timestamp"))
            if created and now - created > PRIVATE_REPLY_WINDOW:
                log(f"SKIP  {comment_id} @{username}: outside the 7-day window")
                if not args.dry_run:
                    ledger.record(comment_id, "expired", username=username,
                                  keyword=entry["keyword"])
                continue

            if args.dry_run:
                log(f"DRY   {comment_id} @{username} -> '{entry['keyword']}' "
                    f"({entry['button_title']} -> {entry['url']})")
                sent += 1
                if args.limit and sent >= args.limit:
                    log("limit reached")
                    return 0
                continue

            # ---- the one-shot send ----
            try:
                resp = client.send_private_reply(comment_id, build_card(entry))
            except GraphError as exc:
                if exc.already_replied:
                    # Someone (or a previous run that died before flushing) already used the
                    # one allowed reply. Terminal, and NOT a failure — record and move on.
                    log(f"DONE  {comment_id} @{username}: already had a reply — recording")
                    ledger.record(comment_id, "already", username=username,
                                  keyword=entry["keyword"])
                    continue
                log(f"FAIL  {comment_id} @{username}: {exc}")
                ledger.record(comment_id, "failed", username=username,
                              keyword=entry["keyword"], error=str(exc))
                failures += 1
                continue

            ledger.record(comment_id, "sent", username=username, keyword=entry["keyword"],
                          message_id=resp.get("message_id"),
                          recipient_id=resp.get("recipient_id"),
                          media_id=post["id"])
            sent += 1
            log(f"SENT  {comment_id} @{username} -> '{entry['keyword']}' "
                f"message_id={resp.get('message_id')}")

            # ---- the visible reply (secondary: a failure here must not mask a sent DM) ----
            if public_reply.get("enabled"):
                try:
                    client.reply_to_comment(comment_id, public_reply["text"])
                    ledger.record(comment_id, "sent", public_replied=True)
                except GraphError as exc:
                    log(f"WARN  {comment_id}: DM sent but public reply failed: {exc}")
                    ledger.record(comment_id, "sent", public_replied=False,
                                  public_reply_error=str(exc))

            if args.limit and sent >= args.limit:
                log("limit reached")
                break
        if args.limit and sent >= args.limit:
            break

    log(f"summary: sent={sent} failures={failures} ledger={ledger.counts()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
