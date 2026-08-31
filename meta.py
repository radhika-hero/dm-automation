"""Graph API access — token bootstrap + the three calls this project makes.

Ported from the scheduler's `publishers/instagram.py` / `publishers/facebook.py` rather than
rewritten (DM_AUTOMATION_PLAN.md §8 step 3). Two tokens are in play and they are NOT
interchangeable:

  * SYSTEM-USER token (`META_SYSTEM_USER_TOKEN`) — reads media + comments, and posts the
    PUBLIC comment reply.
  * PAGE token — derived from the system-user token via /me/accounts, and the ONLY token
    that can send the private reply. See §2 of the plan.

The private reply goes to `POST /<FB_PAGE_ID>/messages`, never to the IG-user endpoint —
that one returns `(#3) Application does not have the capability` (plan §4c).

⚠️ PUBLIC REPO: the token comes from the environment only. Never write it to a file, never
print it, never put it in an error message.
"""
from __future__ import annotations

import json
import os

import requests

GRAPH = "https://graph.facebook.com/v21.0"

# Meta's "this comment already has a private reply" rejection. One-shot and permanent
# (plan §3.1) — so this is a normal outcome to record, not a failure to retry.
ALREADY_REPLIED_SUBCODE = 2534023


class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @property
    def already_replied(self) -> bool:
        return self.subcode == ALREADY_REPLIED_SUBCODE


class MetaClient:
    def __init__(self) -> None:
        self.token = os.environ.get("META_SYSTEM_USER_TOKEN", "")
        self.ig_user_id = os.environ.get("IG_USER_ID", "")
        self.page_id = os.environ.get("FB_PAGE_ID", "")
        self._page_token: str | None = None

    def missing_env(self) -> list[str]:
        return [name for name, value in (
            ("META_SYSTEM_USER_TOKEN", self.token),
            ("IG_USER_ID", self.ig_user_id),
            ("FB_PAGE_ID", self.page_id),
        ) if not value]

    # ---------- low-level ----------

    @staticmethod
    def _unwrap(body: dict, path: str) -> dict:
        if "error" in body:
            err = body["error"]
            raise GraphError(
                f"Graph error on {path}: {err.get('message')}",
                code=err.get("code"),
                subcode=err.get("error_subcode"),
            )
        return body

    def _get(self, path: str, token: str | None = None, **params) -> dict:
        params["access_token"] = token or self.token
        resp = requests.get(f"{GRAPH}/{path}", params=params, timeout=60)
        return self._unwrap(resp.json(), path)

    def _post(self, path: str, token: str | None = None, **data) -> dict:
        data["access_token"] = token or self.token
        resp = requests.post(f"{GRAPH}/{path}", data=data, timeout=60)
        return self._unwrap(resp.json(), path)

    def page_token(self) -> str:
        """Exchange the system-user token for the Page token (facebook.py's _get_page_token)."""
        if self._page_token:
            return self._page_token
        body = self._get("me/accounts", fields="id,access_token")
        for page in body.get("data", []):
            if str(page.get("id")) == str(self.page_id):
                self._page_token = page["access_token"]
                return self._page_token
        raise GraphError(f"Page {self.page_id} not found via /me/accounts")

    # ---------- reads (trigger side, plan §2) ----------

    def recent_media(self, limit: int = 25) -> list[dict]:
        body = self._get(f"{self.ig_user_id}/media",
                         fields="id,caption,permalink,timestamp,comments_count",
                         limit=limit)
        return body.get("data", [])

    def media_by_ids(self, media_ids: list[str]) -> list[dict]:
        out = []
        for media_id in media_ids:
            out.append(self._get(media_id,
                                 fields="id,caption,permalink,timestamp,comments_count"))
        return out

    def comments(self, media_id: str, limit: int = 50) -> list[dict]:
        body = self._get(f"{media_id}/comments",
                         fields="id,text,username,timestamp", limit=limit)
        return body.get("data", [])

    # ---------- Facebook reads ----------
    #
    # Facebook needs the PAGE token for reads too, not the system-user token: /<page>/posts and
    # a post's comments are Page-owned edges. Shapes differ from Instagram in two ways that
    # matter, so they are normalised here rather than in the caller:
    #   * a comment's author is `from.name`, not `username`
    #   * `created_time` is the timestamp field, not `timestamp`
    # Both are renamed to the Instagram spelling so run.py stays one code path.

    def recent_facebook_posts(self, limit: int = 25) -> list[dict]:
        body = self._get(f"{self.page_id}/posts", token=self.page_token(),
                         fields="id,message,created_time,comments.summary(true)", limit=limit)
        out = []
        for post in body.get("data", []):
            out.append({
                "id": post["id"],          # already qualified <page>_<post> on this edge
                "caption": post.get("message") or "",
                "timestamp": post.get("created_time"),
                "comments_count": (post.get("comments", {})
                                   .get("summary", {}).get("total_count", 0)),
            })
        return out

    def facebook_comments(self, post_id: str, limit: int = 50) -> list[dict]:
        body = self._get(f"{post_id}/comments", token=self.page_token(),
                         fields="id,message,from,created_time", limit=limit)
        out = []
        for comment in body.get("data", []):
            author = comment.get("from") or {}
            out.append({
                "id": comment["id"],
                "text": comment.get("message") or "",
                "username": author.get("name", ""),
                # Our OWN comments must never be treated as leads. On Instagram that is a
                # username match; on Facebook the Page comments as itself, so compare ids —
                # the display name can be changed in Page settings at any time and a rename
                # would silently turn the runner's own hashtag comments into "leads".
                "from_id": author.get("id", ""),
                "timestamp": comment.get("created_time"),
            })
        return out

    def reply_to_facebook_comment(self, comment_id: str, text: str) -> dict:
        """The visible reply. On Facebook a reply is a comment ON the comment."""
        return self._post(f"{comment_id}/comments", token=self.page_token(), message=text)

    # ---------- writes ----------

    def send_private_reply(self, comment_id: str, message: dict) -> dict:
        """The one-shot DM. PAGE endpoint + PAGE token (plan §2). Irreversible."""
        return self._post(f"{self.page_id}/messages",
                          token=self.page_token(),
                          recipient=json.dumps({"comment_id": comment_id}),
                          message=json.dumps(message))

    def reply_to_comment(self, comment_id: str, text: str) -> dict:
        """The visible 'Sent it to your DMs' reply. IG comments API, system-user token."""
        return self._post(f"{comment_id}/replies", message=text)


def build_card(entry: dict) -> dict:
    """Generic template with one web_url button — the form Pritam confirmed renders (§1).

    Max 10 elements / 3 buttons per element. `image_url` is optional and was NOT part of the
    2026-08-16 proof, so it stays optional here.
    """
    element = {
        "title": entry["title"],
        "subtitle": entry.get("subtitle", ""),
        "buttons": [{
            "type": "web_url",
            "url": entry["url"],
            "title": entry["button_title"],
        }],
    }
    if entry.get("image_url"):
        element["image_url"] = entry["image_url"]
    return {"attachment": {"type": "template", "payload": {
        "template_type": "generic",
        "elements": [element],
    }}}
