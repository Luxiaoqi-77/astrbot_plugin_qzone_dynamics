"""Identify an owner-authored signature publication from exact recent evidence."""
from __future__ import annotations

import math
import re
import time
from bs4 import BeautifulSoup
from .feeds3_reader import decode_feeds3, feed_post_text
from .comment_reply import normalize_post_id


def matches_signature(content, signature):
    if not isinstance(content, str) or not isinstance(signature, str):
        return False
    value, wanted = "".join(content.split()), "".join(signature.split())
    if not wanted:
        return False
    if value == wanted:
        return True
    prefixes = ("我更新了个性签名：", "我更新了个性签名:", "我的新签名：", "我的新签名:", "个性签名：", "个性签名:")
    return any(value == prefix + wanted for prefix in prefixes)


def match_signature_feed(body, activity, *, now=None):
    now = time.time() if now is None else now
    updated = activity.get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated) or not 0 <= now - updated <= 172800:
        return None
    owner, signature = str(activity.get("account_id", "")), activity.get("signature")
    matches = {}
    for item in decode_feeds3(body):
        if not isinstance(item, dict) or not isinstance(item.get("html"), str):
            continue
        soup = BeautifulSoup(item["html"], "html.parser")
        markers = soup.select('[name="feed_data"]')
        if len(markers) != 1:
            continue
        marker = markers[0]
        if str(marker.get("data-uin", "")) != owner or not marker.has_attr("data-accessright"):
            continue
        summary = soup.select_one(".qz_summary .txt-box-title") or soup.select_one(".qz_summary")
        if summary is None or not matches_signature(feed_post_text(summary, owner), signature):
            continue
        stamp = item.get("abstime", marker.get("data-abstime"))
        if isinstance(stamp, bool) or not re.fullmatch(r"[0-9]{9,12}", str(stamp or "")):
            continue
        stamp = int(stamp)
        if not updated - 120 <= stamp <= min(updated + 21600, now + 120):
            continue
        try:
            post_id = normalize_post_id(marker.get("data-tid"))
        except ValueError:
            continue
        candidate = {"post_id": post_id, "created_at": stamp, "content": feed_post_text(summary, owner)}
        if post_id in matches and matches[post_id] != candidate:
            return None
        matches[post_id] = candidate
    return next(iter(matches.values())) if len(matches) == 1 else None


async def discover_signature_post(transport, activity, *, now=None):
    now = time.time() if now is None else now
    owner = str(activity["account_id"])
    if owner != str(transport.credential_store.account_uin):
        raise ValueError("signature_discovery_account_mismatch")
    body = await transport.request("list_owner_feeds_html", query={
        "uin": owner, "scope": "1", "view": "1", "daylist": "", "uinlist": "", "gid": "",
        "flag": "1", "filter": "all", "applist": "all", "refresh": "1", "pagenum": "1",
        "begintime": "0", "dayspac": "5", "sidomain": "qzonestyle.gtimg.cn", "useutf8": "1",
        "outputhtmlfeed": "1", "rd": "0.3", "windowId": "0.3", "usertime": str(int(now)), "format": "json",
    }, extra_headers={"Referer": f"https://user.qzone.qq.com/{owner}/main", "User-Agent": "Mozilla/5.0"})
    return match_signature_feed(body, activity, now=now)
