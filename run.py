"""Comment → DM poller. One pass: read comments, match keywords, send the card, record it.

    python -X utf8 run.py --dry-run     # read + match + print, send NOTHING
    python -X utf8 run.py               # live
    python -X utf8 run.py --limit 1     # live, but stop after one send (use for the first test)

BOTH Instagram and Facebook since 2026-09-01. Facebook was blocked on `pages_messaging` until
Pritam regenerated the token with that scope and published the app; the design anticipated it, so
turning it on was a scope plus per-platform reads, not a rewrite — the SEND is byte-identical
(POST /<FB_PAGE_ID>/messages with a Page token), because Instagram private replies already route
through the Page.

Three things genuinely differ per platform and are handled explicitly:
  * scope       — Instagram media ids and Facebook post ids are different namespaces, so each
                  entry pins them separately (`media_ids` vs `facebook_post_ids`).
  * own-comment — Instagram matches our username; Facebook compares the Page id, since a Page
                  display name can be renamed at any time.
  * the visible reply — on Facebook a reply is a comment ON the comment, and its wording differs
                  ("Requests folder" means nothing there).

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

    # ---- which platforms to poll ----
    # Facebook joined 2026-09-01, once a regenerated token carried `pages_messaging` and the app
    # was published. Until then the SAME endpoint and the SAME card returned
    # "(#230) Requires pages_messaging" against a real FB comment — a genuine missing scope, not
    # the misleading #230 that a fake comment id produces on Instagram (plan §2a/§4a).
    platforms = [p for p in config.get("platforms", ["instagram", "facebook"])
                 if p in ("instagram", "facebook")]

    sent = 0
    failures = 0
    now = datetime.now(timezone.utc)

    for platform in platforms:
        # ---- pick the posts in scope ----
        if platform == "instagram":
            pinned = config.get("media_ids") or []
            if pinned:
                media = client.media_by_ids(pinned)
                log(f"[instagram] scope: {len(media)} pinned media id(s)")
            else:
                recent = client.recent_media(config.get("scan_media_limit", 25))
                media = select_media(recent, keywords)
                log(f"[instagram] scope: {len(media)} of {len(recent)} recent posts carry a CTA")
        else:
            recent = client.recent_facebook_posts(config.get("scan_media_limit", 25))
            media = select_media(recent, keywords)
            log(f"[facebook] scope: {len(media)} of {len(recent)} recent posts carry a CTA")

        for post in media:
            if not post.get("comments_count"):
                continue
            try:
                comments = (client.comments(post["id"]) if platform == "instagram"
                            else client.facebook_comments(post["id"]))
            except GraphError as exc:
                log(f"FAIL  [{platform}] {post['id']}: could not read comments: {exc}")
                failures += 1
                continue

            for comment in comments:
                comment_id = comment["id"]
                username = (comment.get("username") or "").lower()

                # Our own hashtag first-comment posted by the scheduler — never a lead (plan §2).
                # Instagram identifies us by username; Facebook by the Page's own id, because a
                # Page display name can be renamed at any time.
                if platform == "instagram" and username == own:
                    continue
                if platform == "facebook" and comment.get("from_id") == client.page_id:
                    continue
                # The ledger key is namespaced by platform. A Facebook comment id and an
                # Instagram comment id are different namespaces, and a collision would mean
                # silently skipping a real lead — or worse, believing a DM was already sent.
                ledger_key = f"{platform}:{comment_id}"
                if ledger.is_done(ledger_key):
                    continue

                entry = match_comment(comment.get("text", ""), keywords,
                                      media_id=post["id"], platform=platform)
                if not entry:
                    continue

                created = parse_ts(comment.get("timestamp"))
                if created and now - created > PRIVATE_REPLY_WINDOW:
                    log(f"SKIP  [{platform}] {comment_id} @{username}: outside the 7-day window")
                    if not args.dry_run:
                        ledger.record(ledger_key, "expired", username=username,
                                      keyword=entry["keyword"], platform=platform)
                    continue

                if args.dry_run:
                    log(f"DRY   [{platform}] {comment_id} @{username} -> '{entry['keyword']}' "
                        f"({entry['button_title']} -> {entry['url']})")
                    sent += 1
                    if args.limit and sent >= args.limit:
                        log("limit reached")
                        return 0
                    continue

                # ---- the one-shot send ----
                # Identical call on both platforms: POST /<FB_PAGE_ID>/messages with a Page
                # token, recipient={"comment_id": ...}. Instagram routes through the Page too,
                # which is why adding Facebook needed a scope rather than a new sender.
                try:
                    resp = client.send_private_reply(comment_id, build_card(entry))
                except GraphError as exc:
                    if exc.already_replied:
                        # Someone (or a previous run that died before flushing) already used the
                        # one allowed reply. Terminal, and NOT a failure — record and move on.
                        log(f"DONE  [{platform}] {comment_id} @{username}: "
                            "already had a reply — recording")
                        ledger.record(ledger_key, "already", username=username,
                                      keyword=entry["keyword"], platform=platform)
                        continue
                    log(f"FAIL  [{platform}] {comment_id} @{username}: {exc}")
                    ledger.record(ledger_key, "failed", username=username,
                                  keyword=entry["keyword"], platform=platform, error=str(exc))
                    failures += 1
                    continue

                ledger.record(ledger_key, "sent", username=username, keyword=entry["keyword"],
                              platform=platform,
                              message_id=resp.get("message_id"),
                              recipient_id=resp.get("recipient_id"),
                              media_id=post["id"])
                sent += 1
                log(f"SENT  [{platform}] {comment_id} @{username} -> '{entry['keyword']}' "
                    f"message_id={resp.get('message_id')}")

                # ---- the visible reply (secondary: a failure here must not mask a sent DM) ----
                if public_reply.get("enabled"):
                    # Per-platform wording. Instagram says "check your Requests folder", which is
                    # meaningless on Facebook — one text for both would be wrong on one of them.
                    text = (public_reply.get("facebook_text") or public_reply["text"]
                            if platform == "facebook" else public_reply["text"])
                    try:
                        if platform == "facebook":
                            client.reply_to_facebook_comment(comment_id, text)
                        else:
                            client.reply_to_comment(comment_id, text)
                        ledger.record(ledger_key, "sent", public_replied=True)
                    except GraphError as exc:
                        log(f"WARN  [{platform}] {comment_id}: DM sent but public reply "
                            f"failed: {exc}")
                        ledger.record(ledger_key, "sent", public_replied=False,
                                      public_reply_error=str(exc))

                if args.limit and sent >= args.limit:
                    log("limit reached")
                    break
            if args.limit and sent >= args.limit:
                break
        if args.limit and sent >= args.limit:
            break

    
    log(f"summary: sent={sent} failures={failures} ledger={ledger.counts()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
