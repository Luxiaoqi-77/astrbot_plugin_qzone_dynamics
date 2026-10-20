"""Local exact-ID reply guards and receipts; this module has no network client."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

# Generic page/root counters are not evidence of a whole thread total.
SERVER_COMMENT_TOTAL_FIELDS = ("total_comment_count", "totalCommentCount")
SERVER_COMMENT_TOTAL_PATHS = frozenset(
    f"{source}.{field}"
    for source in ("feed", "feed.data", "detail", "detail.data")
    for field in SERVER_COMMENT_TOTAL_FIELDS
) | {"detail.cmtnum+sum(commentlist.replyNum)"}


def normalize_post_id(value: object) -> str:
    """Validate an exact post tid separately from numeric QQ account IDs.

    Args:
        value: Server or command identifier, including alphanumeric post tids.

    Returns:
        Bounded exact ID used only in encoded query and form fields.

    Raises:
        ValueError: If the identifier is empty or contains unsafe characters.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("post_id_invalid")
    result = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result):
        raise ValueError("post_id_invalid")
    return result


@dataclass(frozen=True)
class VerifiedComment:
    """One exact comment row from a fully loaded post and complete thread."""

    comment_id: str
    author_uin: str
    content: str
    parent_comment_id: str = ""
    identity_key: str = ""


@dataclass(frozen=True)
class ExactReplyTarget:
    """A reply target bound to one account, post, comment, and parent author."""

    account_uin: str
    post_id: str
    parent_comment_id: str
    target_uin: str
    thread_fingerprint: str
    idempotency_key: str
    parent_identity_key: str = ""
    legacy_idempotency_key: str = ""
    # 2026-10-20：QQ空间楼层是两层结构，回复接口只认主评论 ID。
    # 楼内追问的目标 ID 另存，发请求时用 thread_root_comment_id。
    thread_root_comment_id: str = ""


_QZONE_MENTION = re.compile(r"@\{uin:\d+,nick:([^,}]*),who:\d+,auto:\d+\}")


def clean_comment_text(text: str) -> str:
    """Replace Qzone's raw @{uin:...,nick:...} markup with a readable mention."""
    cleaned = _QZONE_MENTION.sub(
        lambda match: f"@{match.group(1)}" if match.group(1) else "", str(text or "")
    )
    return " ".join(cleaned.split())


def strip_trailing_period(text: str) -> str:
    """Drop terminal sentence periods (主人 2026-10-20：评论回复不要用句号收尾)."""
    cleaned = str(text or "").rstrip()
    while cleaned and cleaned[-1] in "。．.·":
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def comment_identity_key(comment_id: str, parent_comment_id: str = "") -> str:
    """Return the internal hierarchy identity while preserving raw IDs."""
    return (
        f"reply:{parent_comment_id}:{comment_id}"
        if parent_comment_id
        else f"top_level:{comment_id}"
    )


def bind_reply_target(
    *,
    account_uin: str,
    post_id: str,
    post_author_uin: str,
    post_content: str,
    comment_id: str,
    expected_target_uin: str,
    declared_comment_count: int,
    comments: tuple[VerifiedComment, ...] | list[VerifiedComment],
    comment_parent_id: str = "",
) -> ExactReplyTarget:
    """Bind a verified exact target and fingerprint the full selected context.

    Args:
        account_uin: Account whose counted thread was loaded.
        post_id: Exact selected post identifier.
        post_author_uin: Verified owner of the selected post.
        post_content: Exact post text used to generate the reviewed reply.
        comment_id: Exact selected comment identifier.
        expected_target_uin: Verified selected comment author.
        declared_comment_count: Explicit server total including replies.
        comments: Normalized complete comment rows with exact parent IDs.
        comment_parent_id: Optional exact parent of the selected comment, used
            only to disambiguate duplicate raw IDs across nesting levels.

    Returns:
        One target with stable context and permanent idempotency fingerprints.

    Raises:
        ValueError: If the counted thread or exact target is invalid.
    """
    fields = (
        account_uin,
        post_id,
        post_author_uin,
        post_content,
        comment_id,
        expected_target_uin,
    )
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ValueError("account, post, comment, and target IDs are required")
    if post_author_uin != account_uin:
        raise ValueError("the loaded post does not belong to the active account")
    if not isinstance(comments, (tuple, list)) or not comments:
        raise ValueError("a complete verified comment list is required")
    if type(declared_comment_count) is not int or declared_comment_count != len(comments):
        raise ValueError("the explicit server total must match the complete comment list")
    identity_values = [
        comment.identity_key or comment_identity_key(comment.comment_id, comment.parent_comment_id)
        for comment in comments
    ]
    if len(set(identity_values)) != len(identity_values):
        raise ValueError("same-level comment identities are duplicated")
    matches = [comment for comment in comments if comment.comment_id == comment_id]
    if comment_parent_id:
        matches = [
            comment
            for comment in matches
            if comment.parent_comment_id == comment_parent_id
        ]
    if len(matches) != 1:
        raise ValueError("the exact comment ID must match one thread row")
    target = matches[0]
    if not target.content.strip() or target.author_uin != expected_target_uin:
        raise ValueError("the expected user does not own the exact comment")
    if target.parent_comment_id:
        # 2026-10-20：楼内追问也允许作回复目标；父评论可以是顶层，也可以是
        # 上一层的回复。QQ真实数据里回复会复用主评论 ID，父 ID 先按顶层解析。
        parent_matches = [
            item for item in comments if item.comment_id == target.parent_comment_id
        ]
        top_level_parents = [
            item for item in parent_matches if not item.parent_comment_id
        ]
        if len(top_level_parents) == 1:
            pass
        elif len(parent_matches) != 1:
            raise ValueError("the exact parent comment is missing or ambiguous")
    root = target
    seen_roots = {target.identity_key or comment_identity_key(target.comment_id, target.parent_comment_id)}
    while root.parent_comment_id:
        candidates = [
            item for item in comments if item.comment_id == root.parent_comment_id
        ]
        top_level_candidates = [
            item for item in candidates if not item.parent_comment_id
        ]
        parent = (
            top_level_candidates[0]
            if len(top_level_candidates) == 1
            else (candidates[0] if len(candidates) == 1 else None)
        )
        if parent is None:
            raise ValueError("the exact parent comment is missing or ambiguous")
        parent_identity = parent.identity_key or comment_identity_key(
            parent.comment_id, parent.parent_comment_id
        )
        if parent_identity in seen_roots:
            raise ValueError("comment parent relationships contain a cycle")
        seen_roots.add(parent_identity)
        root = parent
    canonical = {
        "schema_version": 2,
        "account_uin": account_uin,
        "post_id": post_id,
        "post_author_uin": post_author_uin,
        "post_content": post_content,
        "declared_comment_count": declared_comment_count,
        "comments": [],
    }
    duplicate_raw_ids = len({comment.comment_id for comment in comments}) != len(comments)
    for item in sorted(comments, key=lambda item: (item.parent_comment_id, item.comment_id)):
        row = asdict(item)
        row["identity_key"] = item.identity_key or comment_identity_key(
            item.comment_id, item.parent_comment_id
        )
        if not duplicate_raw_ids:
            row.pop("identity_key", None)
        canonical["comments"].append(row)
    thread_fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    legacy_key_material = f"{account_uin}\0{post_id}\0{comment_id}\0{target.parent_comment_id}"
    legacy_idempotency_key = hashlib.sha256(legacy_key_material.encode()).hexdigest()
    idempotency_key = (
        hashlib.sha256(
            f"hierarchical-single-v3\0{account_uin}\0{post_id}\0"
            f"{target.identity_key or comment_identity_key(target.comment_id)}\0{expected_target_uin}".encode()
        ).hexdigest()
        if duplicate_raw_ids
        else legacy_idempotency_key
    )
    return ExactReplyTarget(
        account_uin=account_uin,
        post_id=post_id,
        parent_comment_id=comment_id,
        target_uin=target.author_uin,
        thread_fingerprint=thread_fingerprint,
        idempotency_key=idempotency_key,
        parent_identity_key=target.identity_key or comment_identity_key(target.comment_id),
        legacy_idempotency_key=legacy_idempotency_key,
        thread_root_comment_id=root.comment_id,
    )


class CommentReplyReceipts:
    """Persist single-attempt exact-target reply claims without retrying unknown outcomes."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("reply receipt store is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("receipts"), dict):
            raise RuntimeError("reply receipt store has an invalid shape")
        return value["receipts"]

    def _save(self, receipts: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "receipts": receipts}, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def contains(self, target: ExactReplyTarget) -> bool:
        """Check all legacy and current claim keys before generating a reply."""
        with self.lock:
            receipts = self._load()
            return any(key and key in receipts for key in (
                target.idempotency_key, target.legacy_idempotency_key,
            ))

    def claim(self, target: ExactReplyTarget) -> bool:
        """Claim one reply; every prior status permanently prevents a new send."""
        with self.lock:
            receipts = self._load()
            keys = {target.idempotency_key}
            if target.legacy_idempotency_key:
                keys.add(target.legacy_idempotency_key)
            if any(key in receipts for key in keys):
                return False
            receipts[target.idempotency_key] = {
                "account_uin": target.account_uin,
                "post_id": target.post_id,
                "parent_comment_id": target.parent_comment_id,
                "parent_identity_key": target.parent_identity_key,
                "target_uin": target.target_uin,
                "thread_fingerprint": target.thread_fingerprint,
                "status": "sending",
            }
            self._save(receipts)
            return True

    def finish(self, target: ExactReplyTarget, *, status: str) -> None:
        """Record success or transport uncertainty; neither state can be retried."""
        if status not in {"sent", "delivery_unknown"}:
            raise ValueError("reply outcome must be sent or delivery_unknown")
        with self.lock:
            receipts = self._load()
            receipt = receipts.get(target.idempotency_key)
            if not receipt or receipt.get("status") != "sending":
                raise RuntimeError("reply has no active local claim")
            receipt["status"] = status
            self._save(receipts)
