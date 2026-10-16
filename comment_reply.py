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
)


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
    matches = [
        comment
        for comment in comments
        if comment.comment_id == comment_id and not comment.parent_comment_id
    ]
    if len(matches) != 1:
        raise ValueError("the exact comment ID must match one thread row")
    target = matches[0]
    if not target.content.strip() or target.author_uin != expected_target_uin:
        raise ValueError("the expected user does not own the exact comment")
    if target.parent_comment_id:
        parent_matches = [
            item
            for item in comments
            if item.comment_id == target.parent_comment_id and not item.parent_comment_id
        ]
        if len(parent_matches) != 1:
            raise ValueError("the exact parent comment is missing or ambiguous")
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
