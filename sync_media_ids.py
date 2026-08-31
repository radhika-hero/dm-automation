"""Pin every keyword to its posts using NOTION as the source of truth, then enable what is ready.

    python -X utf8 sync_media_ids.py              # dry run — prints the plan, writes nothing
    python -X utf8 sync_media_ids.py --write      # writes keywords.json
    python -X utf8 sync_media_ids.py --write --no-enable   # pin only, touch no 'enabled' flag

WHY NOTION AND NOT THE CAPTION (rewritten 2026-09-01)
    A caption's CTA tells you WHAT WORD the viewer was asked to type. It does NOT tell you WHICH
    REEL the post is. Four separate series (Reel Script N, read_the_label, swad_se_sehat_tak,
    Evergreen) all use LABEL as their CTA, so caption-reading cannot separate SCR-04's reel from
    SCR-07's — and on 2026-08-31 it did not: all three live LABEL posts were pinned to BOTH
    entries, entry 2 could never fire, and SCR-07's own reel was serving entry 1's guide.

    Notion knows the answer and always did. Every posted row carries `Source File` — the exact
    disk path of the media that went out ("...\\Reel Script 7 new\\Final\\Hook1_Final.mp4") — and,
    since 2026-08-31, `Media ID`. That is provenance and identity in one row, which is precisely
    what pinning needs and what nothing else holds. schedule.json cannot serve: runner.py prunes
    terminal rows after 7 days.

    So the chain is now deterministic, with no text-guessing anywhere in it:

        Notion row -> Source File path -> piece code (SCR-07) -> the entry whose `pieces` claims it

    The caption CTA reader is KEPT, demoted to a cross-check that WARNS when it disagrees with
    the provenance. That disagreement is exactly what exposed the LABEL bug, so it earns its keep.

AUTHORITATIVE, NOT APPEND-ONLY
    The old version could only ever ADD ids, because a caption guess was never trustworthy enough
    to remove one on. Notion is, so this computes the correct set and reports REMOVALS too — which
    is what finally lets a wrongly-pinned post be taken off an entry. Entries with an empty
    `pieces` (the product page) are never touched automatically; pin those by hand.

⚠️ READ-ONLY against Meta and Notion. Fetches media, captions and rows. Sends no DM, posts no
comment, writes nothing to either API.
⚠️ DRY BY DEFAULT — the old script wrote unless told not to. Reversed deliberately: this one can
now remove pins, so the safe direction changed with it.
⚠️ TOKENS come from files at the workspace root and are never printed, logged, or written into
keywords.json. Keep them out of git.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

from matcher import matches  # noqa: F401  (kept for parity with the cross-check below)

GRAPH = "https://graph.facebook.com/v21.0"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DATABASE_ID = "83c975e1-e9af-822f-9311-811fc9bad224"   # 📆 Content Calendar

CONFIG_PATH = Path(__file__).with_name("keywords.json")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_META_TOKEN = ROOT / "fb_access_token.txt"
DEFAULT_NOTION_TOKEN = ROOT / "notion_token.txt"


# --- provenance: a Source File path -> a piece code ----------------------------------------
# Deliberately a small table of EXPLICIT patterns rather than anything clever. Each one is a
# naming convention Radhika actually uses on disk; a path that matches none of them yields no
# piece, and an unmatched path is REPORTED rather than guessed at. A wrong guess here pins a post
# to the wrong guide, which is the whole class of bug this file exists to end.
PIECE_PATTERNS = [
    (re.compile(r"reel\s*script\s*(\d+)", re.I),                     "SCR"),
    (re.compile(r"series_work[\\/]read_the_label[\\/]part_(\d+)", re.I), "RTL"),
    (re.compile(r"series_work[\\/]swad_se_sehat_tak[\\/]part_(\d+)", re.I), "SST"),
    (re.compile(r"series_work[\\/]behind_the_dish[\\/]part_(\d+)", re.I),   "BD"),
    (re.compile(r"[\\/]Video\s*(\d+)[\\/]", re.I),                   "VID"),
]


def piece_of(source_file: str) -> str | None:
    """'...\\Reel Script 7 new\\Final\\Hook1_Final.mp4' -> 'SCR-07'. None if it is not a piece."""
    for pattern, prefix in PIECE_PATTERNS:
        m = pattern.search(source_file or "")
        if m:
            return f"{prefix}-{int(m.group(1)):02d}"
    return None


def read_token(path: Path, env_var: str) -> str:
    path = Path(os.environ.get(env_var, path))
    if not path.exists():
        raise SystemExit(f"ERROR: token file not found: {path.name} (set {env_var})")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"ERROR: {path.name} is empty")
    return token


def scrub(text: str, *tokens: str) -> str:
    for token in tokens:
        if token:
            text = text.replace(token, "<token>")
    return text


# --- Notion ---------------------------------------------------------------------------------

def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    if prop.get("type") == "url":
        return prop.get("url") or ""
    return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))


def fetch_notion_posts(token: str) -> list[dict]:
    """Every posted Instagram row that has a Media ID, with its provenance."""
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
               "Content-Type": "application/json"}
    rows, cursor = [], None
    while True:
        payload = {"filter": {"and": [
            {"property": "Platform", "select": {"equals": "Instagram"}},
            {"property": "Status", "select": {"equals": "Posted"}},
        ]}, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        body = requests.post(f"{NOTION_API}/databases/{DATABASE_ID}/query",
                             headers=headers, json=payload, timeout=60).json()
        if body.get("object") == "error":
            raise SystemExit(f"ERROR from Notion: {scrub(str(body.get('message')), token)}")
        for page in body.get("results", []):
            props = page.get("properties", {})
            media_id = _plain(props.get("Media ID"))
            if not media_id:
                continue   # a deleted/archived post, or one predating the backfill
            rows.append({
                "post_id": _plain(props.get("Post ID")),
                "media_id": media_id,
                "source_file": _plain(props.get("Source File")),
            })
        if not body.get("has_more"):
            return rows
        cursor = body.get("next_cursor")


# --- Instagram (cross-check only) -----------------------------------------------------------

# "Comment COOKIE and I'll DM you ...", "Comment karo 'KHAJOOR' - FREE guide milegi",
# "Comment OLD IS GOLD - paanchon aadatein DM kar dungi". The trigger is the run of capitals
# straight after the word comment, and it may be several words (OLD IS GOLD).
# The (?i:...) scoping is deliberate: a plain re.IGNORECASE would also make [A-Z] match
# lowercase, and the whole point is that the trigger is the SHOUTED word.
CTA_RE = re.compile(r"\b(?i:comment)\s+(?i:karo\s+)?[\"'“‘]?([A-Z][A-Z]*(?:\s+[A-Z]+)*)")


def cta_triggers(caption: str) -> set[str]:
    out = set()
    for raw in CTA_RE.findall(caption or ""):
        word = " ".join(raw.split()).strip(" '\"”’")
        if len(word) >= 3:          # a single stray capital ("Comment I") is noise
            out.add(word.upper())
    return out


def graph_get(path: str, token: str, **params) -> dict:
    params["access_token"] = token
    body = requests.get(f"{GRAPH}/{path}", params=params, timeout=60).json()
    if "error" in body:
        raise SystemExit(f"ERROR from Graph: {scrub(str(body['error'].get('message')), token)}")
    return body


def fetch_captions(token: str, limit: int) -> dict[str, str]:
    """media id -> caption, for the CTA cross-check and the orphan report."""
    for page in graph_get("me/accounts", token,
                          fields="id,instagram_business_account").get("data", []):
        ig = (page.get("instagram_business_account") or {}).get("id")
        if ig:
            break
    else:
        raise SystemExit("ERROR: no Instagram business account linked to this token")

    out: dict[str, str] = {}
    body = graph_get(f"{ig}/media", token, fields="id,caption", limit=50)
    while True:
        for item in body.get("data", []):
            out[item["id"]] = item.get("caption") or ""
        nxt = body.get("paging", {}).get("next")
        if not nxt or len(out) >= limit:
            break
        body = requests.get(nxt, timeout=60).json()
        if "error" in body:
            break
    return out


def write_back(config: dict) -> None:
    """Rewrite keywords.json from the parsed config.

    Schema v2 (2026-08-31) made this a plain `json.dumps`. Under v1 it could not be: the file
    carried hand-maintained formatting and long prose `_note` blocks, so a reflow would have made
    every future diff unreadable, and this function had to splice individual fields as TEXT by
    regex. v2 is machine-generated with a fixed field order and prose confined to `notes`, so a
    full round-trip is now lossless AND stable — the same input always produces the same bytes.

    That is what makes a UI tool possible: any writer (this script, a future editor, a human)
    can load, mutate and dump the whole file without needing to know how it was laid out.
    """
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)  # fail loudly here rather than at send time
    CONFIG_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write keywords.json (default: dry run)")
    ap.add_argument("--no-enable", action="store_true",
                    help="pin only; leave every 'enabled' flag exactly as it is")
    ap.add_argument("--limit", type=int, default=400, help="how many posts to read captions for")
    args = ap.parse_args()

    notion_token = read_token(DEFAULT_NOTION_TOKEN, "DS_NOTION_TOKEN_FILE")
    meta_token = read_token(DEFAULT_META_TOKEN, "DS_TOKEN_FILE")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keywords = config.get("keywords", [])

    posts = fetch_notion_posts(notion_token)
    captions = fetch_captions(meta_token, args.limit)
    print(f"Notion: {len(posts)} posted Instagram row(s) with a Media ID")
    print(f"Instagram: {len(captions)} caption(s) read for cross-check\n")

    # piece code -> the media ids of every post made from it
    by_piece: dict[str, list[str]] = {}
    unknown: list[dict] = []
    for post in posts:
        piece = piece_of(post["source_file"])
        if piece:
            by_piece.setdefault(piece, []).append(post["media_id"])
        else:
            unknown.append(post)

    # ⚠️ NOTION IS AUTHORITATIVE ONLY OVER POSTS IT KNOWS ABOUT. It holds rows for what the
    # SCHEDULER published — 32 Instagram rows — while the account itself carries 323 media. The
    # rest were posted manually or predate the scheduler, and Notion has no opinion on them at
    # all. Treating "absent from Notion" as "does not belong" deletes perfectly good pins: a
    # first pass of this rewrite stripped 10 real posts off CHEMICAL for exactly that reason.
    #
    # So a pin is removed ONLY when Notion KNOWS that post and places it elsewhere. A pin Notion
    # has never seen is left alone for a human to judge.
    notion_known = {post["media_id"] for post in posts}

    changes, enabled_now, still_off, auto_off = [], [], [], []
    for entry in keywords:
        pieces = entry.get("pieces") or []
        if not pieces:
            continue     # e.g. the product page — pinned by hand, never touched here
        wanted = [mid for piece in pieces for mid in by_piece.get(piece, [])]
        # Deliberate hand-pins. Never derived, never removed — an entry may legitimately answer a
        # post it did not produce (an Evergreen carousel whose CTA is LABEL, say).
        extra = [m for m in (entry.get("extra_media_ids") or []) if m not in wanted]
        existing = entry.get("media_ids") or []
        kept_unknown = [m for m in existing
                        if m not in notion_known and m not in wanted and m not in extra]
        added = [m for m in wanted if m not in existing]
        removed = [m for m in existing
                   if m in notion_known and m not in wanted and m not in extra]
        merged = wanted + extra + kept_unknown
        if added or removed:
            changes.append((entry["id"], added, removed, kept_unknown))
        entry["media_ids"] = merged

    print("pins (Notion provenance is authoritative for posts Notion knows):")
    for eid, added, removed, kept in changes:
        for m in added:
            print(f"  + {eid:22} {m}")
        for m in removed:
            print(f"  - {eid:22} {m}   <- Notion says this post is not from {eid}")
        if kept:
            print(f"    {eid:22} kept {len(kept)} pin(s) Notion has no row for (manual posts)")
    if not changes:
        print("  (nothing to change)")

    # --- enable/disable ---------------------------------------------------------------------
    for entry in keywords:
        # An ENABLED entry with NO media_ids is unscoped, and unscoped means it answers its word
        # on ANY post carrying any CTA. Verified live 2026-08-31: with SWEET enabled and unpinned,
        # a comment of "so sweet" on a label-reading reel returned the SWEET card.
        if not args.no_enable and entry.get("enabled") and not entry.get("media_ids"):
            entry["enabled"] = False
            auto_off.append(f"{entry['id']} ({entry.get('title','')})")
            continue
        if args.no_enable or entry.get("enabled"):
            continue
        if not entry.get("url"):
            still_off.append(f"{entry['id']}: no url yet")
        elif not entry.get("media_ids"):
            still_off.append(f"{entry['id']}: no post from {entry.get('pieces') or 'its piece'} yet")
        elif entry.get("blocked_reason"):
            still_off.append(f"{entry['id']}: {entry['blocked_reason']}")
        else:
            entry["enabled"] = True
            enabled_now.append(f"{entry['id']} ({entry.get('title','')})")

    if enabled_now:
        print("\nenabled:")
        for line in enabled_now:
            print(f"  ON   {line}")
    if auto_off:
        print("\nauto-disabled (enabled but unpinned — would answer on ANY post):")
        for line in auto_off:
            print(f"  off  {line}")
    if still_off:
        print("\nleft disabled:")
        for line in still_off:
            print(f"  off  {line}")

    # --- cross-check: does the caption's CTA agree with the provenance? ----------------------
    # Kept precisely because this disagreement is what exposed the LABEL misrouting. It WARNS;
    # it never decides.
    pinned_to = {mid: e for e in keywords for mid in (e.get("media_ids") or [])}
    disagreements = []
    for media_id, caption in captions.items():
        asked = cta_triggers(caption)
        entry = pinned_to.get(media_id)
        if entry and asked and entry["keyword"].upper() not in asked:
            disagreements.append((media_id, entry["id"], sorted(asked)))
    if disagreements:
        print("\n⚠️  CROSS-CHECK: the caption asks for a different word than the pin implies.")
        print("   Provenance wins — but check the caption is not asking for the wrong thing:")
        for media_id, eid, asked in disagreements:
            print(f"  {media_id}  pinned to {eid}, caption says {', '.join(asked)}")

    configured = {t.upper() for e in keywords for t in [e["keyword"], *e.get("aliases", [])]}
    orphans: dict[str, int] = {}
    for caption in captions.values():
        for word in cta_triggers(caption) - configured:
            orphans[word] = orphans.get(word, 0) + 1
    if orphans:
        print("\n⚠️  CTAs in live captions with NO keyword configured "
              "(people commenting these get nothing):")
        for word, count in sorted(orphans.items(), key=lambda kv: -kv[1]):
            print(f"  {word:16} asked for on {count} post(s)")

    if unknown:
        print(f"\nnote: {len(unknown)} posted row(s) have a Source File that maps to no piece "
              "(statics, carousels, festivals) — expected, nothing to pin:")
        for post in unknown[:5]:
            print(f"  {post['post_id']}")
        if len(unknown) > 5:
            print(f"  … and {len(unknown) - 5} more")

    if not args.write:
        print("\nDRY RUN — keywords.json NOT written. Re-run with --write to apply.")
        return 0

    config["keywords"] = keywords
    write_back(config)
    print(f"\nwrote {CONFIG_PATH.name}. Run validate.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
