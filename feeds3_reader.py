"""Bounded, data-only parsing of the current Qzone owner-feed response."""

from __future__ import annotations

import re
import time

import json5
from bs4 import BeautifulSoup

from .comment_reply import build_reply_content, comment_identity_key
from .comment_transport import MAX_RESPONSE_BYTES


class Feeds3ReadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def decode_feeds3(body):
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise Feeds3ReadError("feeds3_response_invalid")
    try:
        text = body.decode("utf-8-sig").strip()
        if not text.startswith("{"):
            match = re.fullmatch(r"[\w$.]+\s*\(\s*(\{.*\})\s*\)\s*;?", text, re.S)
            if not match:
                raise ValueError("wrapper")
            text = match.group(1)
        # This endpoint emits JS object literals with bare undefined values.
        # Preserve every quoted string; never evaluate JavaScript.
        text = re.sub(
            r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\bundefined\b""",
            lambda m: m.group(1) or "null",
            text,
        )
        value = json5.loads(text, allow_duplicate_keys=False)
        if (
            not isinstance(value, dict)
            or type(value.get("code")) is not int
            or value["code"] != 0
        ):
            raise ValueError("business status")
        items = value.get("data", {}).get("data")
        if not isinstance(items, list) or len(items) > 200:
            raise ValueError("items")
        return items
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise Feeds3ReadError("feeds3_response_invalid") from None


def _decimal(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,32}", str(value or "")):
        raise Feeds3ReadError("feeds3_identity_invalid")
    return str(value)


def _comment(node, parent=""):
    cid = _decimal(node.get("data-tid"))
    author = _decimal(node.get("data-uin"))
    content = node.select_one(".comments-content")
    if content is None:
        raise Feeds3ReadError("feeds3_comment_content_missing")
    content = BeautifulSoup(str(content), "html.parser")
    for controls in content.select(
        ".comments-op, .comments-list, .mod-comments-sub, .act-reply"
    ):
        controls.decompose()
    author_link = content.find("a", attrs={"link": "nameCard_" + author})
    if author_link:
        author_link.decompose()
    targets = content.select('a[link^="nameCard_"]')
    target = targets[0] if targets and parent else None
    target_uin = target.get("link", "").removeprefix("nameCard_") if target else ""
    target_name = target.get_text(strip=True) if target else ""
    if target:
        target.decompose()
    for image in content.find_all("img"):
        image.replace_with(image.get("alt", ""))
    text = content.get_text(" ", strip=True)
    text = re.sub(r"^(?:回复\s*)?[:：\s]+", "", text).strip()
    if target_uin:
        text = re.sub(r"^回复\s*[:：]?\s*", "", text)
        text = build_reply_content(text, _decimal(target_uin), target_name)
    if not text or len(text) > 4000:
        raise Feeds3ReadError("feeds3_comment_content_invalid")
    return {
        "commentid": cid,
        "uin": author,
        "content": text,
        "name": node.get("data-nick", ""),
        "parent_comment_id": parent,
        "comment_identity_key": comment_identity_key(cid, parent),
    }


def parse_feeds3_thread(body, owner, post, *, max_comments=20, server_count=None):
    snapshots = []
    for item in decode_feeds3(body):
        if not isinstance(item, dict) or not isinstance(item.get("html"), str):
            continue
        soup = BeautifulSoup(item["html"], "html.parser")
        markers = soup.select('[name="feed_data"]')
        # Activity/like/comment notifications can repeat the same tid. Only
        # the original owner post card contains its own visibility attribute.
        selected = [
            m
            for m in markers
            if m.get("data-tid") == post
            and m.get("data-uin") == owner
            and m.has_attr("data-accessright")
        ]
        if not selected:
            continue
        if len(selected) != 1 or len(markers) != 1:
            raise Feeds3ReadError("feeds3_post_ambiguous")
        summary = soup.select_one(".qz_summary .txt-box-title") or soup.select_one(
            ".qz_summary"
        )
        if summary is None or not summary.get_text(strip=True):
            raise Feeds3ReadError("feeds3_post_content_missing")
        if any(
            re.search(
                r"(?:更多|全部|查看).{0,5}(?:评论|回复)|(?:评论|回复).{0,5}(?:更多|全部)",
                a.get_text(),
            )
            for a in soup.find_all("a")
        ):
            raise Feeds3ReadError("feeds3_thread_truncated")
        roots = soup.select('[data-type="commentroot"]')
        rows = []
        for root in roots:
            rows.append(_comment(root))
            for reply in root.select('[data-type="replyroot"]'):
                rows.append(_comment(reply, _decimal(root.get("data-tid"))))
        if len(soup.select('[data-type="replyroot"]')) != len(rows) - len(roots):
            raise Feeds3ReadError("feeds3_reply_parent_missing")
        count = item.get("commentcnt")
        evidence_key = "feeds3.commentcnt"
        if server_count is not None:
            # Live readback confirms CS counts root comments, not their replies.
            # As in the legacy feed path, root count plus embedded replies is
            # usable only when no "more replies/comments" truncation control exists.
            if type(server_count) is not int or server_count != len(roots):
                raise Feeds3ReadError("feeds3_thread_incomplete")
            count = server_count + len(rows) - len(roots)
            evidence_key = (
                "feeds3.opcnt2.CS+embedded_replies"
                if len(rows) > len(roots)
                else "feeds3.opcnt2.CS"
            )
        if isinstance(count, bool) or not re.fullmatch(r"\d{1,5}", str(count)):
            raise Feeds3ReadError("feeds3_count_missing")
        count = int(count)
        if count != len(rows) or len(rows) > max_comments:
            raise Feeds3ReadError("feeds3_thread_incomplete")
        if len({r["comment_identity_key"] for r in rows}) != len(rows):
            raise Feeds3ReadError("feeds3_comment_ambiguous")
        snapshots.append(
            {
                "account_uin": owner,
                "post_id": post,
                "post_author_uin": owner,
                "post_content": summary.get_text(" ", strip=True),
                "declared_comment_count": count,
                "server_count_evidence": {evidence_key: count},
                "comments": rows,
            }
        )
    if not snapshots:
        raise Feeds3ReadError("feeds3_post_not_found")
    if any(s != snapshots[0] for s in snapshots[1:]):
        raise Feeds3ReadError("feeds3_post_ambiguous")
    return snapshots[0]


async def load_feeds3_thread(transport, owner, post, *, max_comments):
    body = await transport.request(
        "list_owner_feeds_html",
        query={
            "uin": owner,
            "scope": "1",
            "view": "1",
            "daylist": "",
            "uinlist": "",
            "gid": "",
            "flag": "1",
            "filter": "all",
            "applist": "all",
            "refresh": "1",
            "pagenum": "1",
            "begintime": "0",
            "dayspac": "5",
            "sidomain": "qzonestyle.gtimg.cn",
            "useutf8": "1",
            "outputhtmlfeed": "1",
            "rd": "0.3",
            "windowId": "0.3",
            "usertime": str(int(time.time())),
            "format": "json",
        },
        extra_headers={
            "Referer": f"https://user.qzone.qq.com/{owner}/main",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        },
    )
    # The cards often omit their counters. Obtain the independent server count
    # for this exact post rather than calling a visible preview a full thread.
    from .comment_reader import decode_qzone_jsonp

    unikey = f"http://user.qzone.qq.com/{owner}/mood/{post}"
    count_body = await transport.request(
        "get_post_counts",
        query={
            "unikey": unikey,
            "face": "0",
            "fupdate": "1",
            "_stp": str(int(time.time() * 1000)),
        },
        extra_headers={"Referer": f"https://user.qzone.qq.com/{owner}/main"},
    )
    counts = decode_qzone_jsonp(count_body)
    try:
        entries = counts["data"]
        if (
            counts["code"] != 0
            or len(entries) != 1
            or entries[0]["current"]["key"] != unikey
        ):
            raise ValueError("counter identity")
        count = entries[0]["current"]["newdata"]["CS"]
        if type(count) is not int or count < 0:
            raise ValueError("counter")
    except (KeyError, TypeError, ValueError):
        raise Feeds3ReadError("feeds3_count_invalid") from None
    return parse_feeds3_thread(
        body, owner, post, max_comments=max_comments, server_count=count
    )
