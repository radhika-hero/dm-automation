# Diet Swad — Comment → DM

Someone comments a keyword on a Diet Swad Instagram post → they get a DM with a tappable
button to a link. The ManyChat mechanism, in-house, on infrastructure we already own, at ₹0.
**ManyChat itself is not used at all** — this is the whole thing. The button opens a **dietswad.in
blog article**, and that article carries the PDF download.

**Design + all the hard-won API findings live in the scheduler workspace's
`DM_AUTOMATION_PLAN.md`. Read that before changing anything here.** This README is the
operating manual only.

## ⚠️ The three rules

1. **A private reply is ONE-SHOT AND PERMANENT.** One attempt per lead, no retry, no undo.
   A second try returns subcode `2534023`. The message must be right the first time —
   so always `--dry-run` first, and do the first live send with `--limit 1`.
2. **The ledger is the safety net.** `ledger.json` is committed on every run. If it is lost,
   leads get double-attempted (harmlessly rejected) and, worse, the record of who was
   answered is gone. Never `.gitignore` it.
3. **Public repo ⇒ the token never touches a file.** `META_SYSTEM_USER_TOKEN` is an Actions
   secret and nothing else. It is the same never-expiring token that runs the live poster.

## Status

- **Instagram: works today.** No App Review, no new permission, no webhook server.
  `instagram_manage_comments` on the existing token covers it. Card + button render confirmed
  by Pritam 2026-08-17.
- **Facebook Messenger: blocked** on `pages_messaging` (real App Review). Same endpoint, same
  template — `platform` is a parameter here, so adding it later is config, not a rewrite.

## Files

| File | Purpose |
|---|---|
| `run.py` | The poller. Read comments → match → send the card → record. Exits non-zero on failure. |
| `keywords.json` | Keyword → card config. **Rewritten 2026-08-31 against the reel CAPTION FILES, which outrank `lead_magnets\INDEX.md` — the recorded audio is the keyword.** 21 triggers, **13 enabled** and pointing at live blog articles; the 8 disabled ones each carry a `_note` saying what is missing. `OLD IS GOLD` replaces DADA+DADI, `KIDS` replaces BACHCHA, `LABEL` is two `media_ids`-scoped entries (SCR-04 and SCR-07), `MITHAI` deleted, `CHEMICAL` added. |
| | **`media_ids` on an entry** pins it to specific posts — the only safe way two entries can share one trigger. `validate.py` enforces it. |
| `meta.py` | Graph calls + the system-user → Page token exchange, ported from the scheduler's publishers. |
| `ledger.py` | The durable dedupe ledger. |
| `matcher.py` | Word-boundary keyword matching + CTA-caption scoping. |
| `validate.py` | Fail-closed config gate. Run before every push. |
| `sync_media_ids.py` | **Run this after publishing new reels.** Pins every keyword to the posts that actually ask for it, then auto-enables what is ready. Read-only against Meta; appends, never removes. Added 2026-08-31. |
| `list_media.py` | Read-only. Dumps recent IG posts with their media ids and flags every caption that matches more than one trigger, which is exactly the set needing `media_ids`. `--json` emits `{trigger: [id, ...]}` ready to paste. Added 2026-08-31. |
| `tests/test_dm.py` | 20 tests, no network. |

## Running it

```bash
python -X utf8 validate.py            # exit 0 = safe
python -X utf8 sync_media_ids.py --dry-run   # read-only, shows what it would change
python -X utf8 sync_media_ids.py             # pin + auto-enable what is ready
python -X utf8 list_media.py          # media ids + which triggers each caption matches
python -X utf8 list_media.py --json   # {trigger: [media_id, ...]} to paste into keywords.json
python -X utf8 run.py --dry-run       # read + match, send NOTHING
python -X utf8 run.py --limit 1       # live, one send — how the first live test is done
python -X utf8 run.py                 # live
```

Environment: `META_SYSTEM_USER_TOKEN`, `IG_USER_ID`, `FB_PAGE_ID`.

## Scope

Only posts whose **caption carries the CTA** are scanned — a post is in scope when its caption
mentions a configured keyword ("comment BITES and I'll send it"). No hand-maintained media-id
list. Pin an exact list with `media_ids` in the config to override.

Our own hashtag first-comments (`username == dietswad`) are always skipped, and comments
older than the 7-day private-reply window are marked `expired` rather than retried forever.

## Before this fires even once

As of 2026-08-16 the last 6 Instagram posts had **1, 1, 0, 1, 1, 0** comments — every one of
them our own hashtag first-comment. **There are effectively zero real audience comments to
trigger on.** This feature converts comment volume into leads; it does not create comments.

The CTA line in the caption is what generates them. Ship
*"comment BITES and I'll send it to you"* in upcoming captions
(`..\Content Generation\COPY_BANK.md`) or this runs and does nothing, correctly, forever.


## 🔴 `DIET` must be pinned before it is ever enabled

Added 2026-08-31, because this is not obvious from reading the config.

The word **"diet" sits inside "Diet Swad"**, which almost every caption contains.
`caption_has_cta()` matches captions with `contains`, so enabling the `DIET` entry with an empty
`media_ids` pulls essentially *every* post into scope. `match_comment()` then word-matches
"diet", so an ordinary comment such as *"love diet swad"* would fire a DM carrying the snack-plan
card. The DM is one-shot and irreversible, so this is not a mistake that can be tidied up after.

Fill `media_ids` with SCR-16's post ids first (`list_media.py --json`), then enable.

The same pinning requirement applies to the two `LABEL` entries, but there `validate.py` already
refuses to let it through. `DIET` has no such guard, because from the validator's point of view
it is just an ordinary unique trigger.
