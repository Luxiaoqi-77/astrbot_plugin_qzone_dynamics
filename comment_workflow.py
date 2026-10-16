"""Disabled-by-default owner-scoped Qzone comment reply workflow.

The reader supplies explicit server counters and exact comment relationships.
Missing evidence blocks generation and confirmation. Tests use local fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .comment_reply import (
    SERVER_COMMENT_TOTAL_PATHS,
    CommentReplyReceipts,
    ExactReplyTarget,
    VerifiedComment,
    bind_reply_target,
    comment_identity_key,
    normalize_post_id,
)

_UIN = re.compile(r"[0-9]{5,12}\Z")
_EXACT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _identifier(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be an exact string or integer ID")
    result = str(value).strip()
    if not _EXACT_ID.fullmatch(result):
        raise ValueError(f"{field} is missing or malformed")
    return result


def _uin(value: object, field: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{field} must be a numeric QQ UIN")
    result = str(value).strip()
    if result[:1].casefold() == "o":
        result = result[1:]
    if not _UIN.fullmatch(result):
        raise ValueError(f"{field} must be a numeric QQ UIN")
    return result


def normalize_comment_thread(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Normalize a counted server thread and verify its exact relationships.

    Args:
        snapshot: Selected owner post, explicit server counters and comment rows.

    Returns:
        Exact identities and a deterministically ordered comment tuple.

    Raises:
        ValueError: If counters, identities, rows or relationships are unverified.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError("comment thread snapshot is invalid")
    account_uin = _uin(snapshot.get("account_uin"), "account_uin")
    post_author_uin = _uin(snapshot.get("post_author_uin"), "post_author_uin")
    post_id = normalize_post_id(snapshot.get("post_id"))
    post_content = snapshot.get("post_content")
    if not isinstance(post_content, str) or not post_content.strip():
        raise ValueError("the complete post content is missing")
    raw_comments = snapshot.get("comments")
    declared_count = snapshot.get("declared_comment_count")
    evidence = snapshot.get("server_count_evidence")
    if not isinstance(raw_comments, list):
        raise ValueError("the complete comment list is missing")
    if (
        type(declared_count) is not int
        or declared_count < 0
        or not isinstance(evidence, Mapping)
        or not evidence
        or any(
            not isinstance(path, str) or path not in SERVER_COMMENT_TOTAL_PATHS
            or type(value) is not int or value != declared_count
            for path, value in evidence.items()
        )
        or declared_count != len(raw_comments)
    ):
        raise ValueError("explicit server total evidence is absent, invalid or incomplete")

    comments: list[VerifiedComment] = []
    for row in raw_comments:
        if not isinstance(row, Mapping):
            raise ValueError("a comment row is malformed")
        # These are exact source fields observed in the community parser, with
        # explicit aliases for adapter-normalized fixtures. Conflicts fail shut.
        comment_id = _one_alias(row, ("commentid", "comment_id"), "comment ID")
        author_uin = _one_alias(row, ("uin", "author_uin"), "comment author")
        content = _one_alias(row, ("content", "text"), "comment content")
        parent_id = _one_optional_alias(
            row,
            ("parent_comment_id", "parentCommentId", "replyid", "reply_comment_id"),
            "parent comment ID",
        )
        if not isinstance(content, str):
            raise ValueError("comment content is malformed")
        normalized_comment_id = _identifier(comment_id, "comment ID")
        normalized_parent_id = _identifier(parent_id, "parent comment ID") if parent_id else ""
        expected_identity_key = comment_identity_key(normalized_comment_id, normalized_parent_id)
        supplied_identity_key = row.get("comment_identity_key", expected_identity_key)
        if supplied_identity_key != expected_identity_key:
            raise ValueError("comment identity does not match its raw IDs and parent")
        comments.append(
            VerifiedComment(
                comment_id=normalized_comment_id,
                author_uin=_uin(author_uin, "comment author"),
                content=content,
                parent_comment_id=normalized_parent_id,
                identity_key=expected_identity_key,
            )
        )
    identities = [comment.identity_key for comment in comments]
    if len(set(identities)) != len(identities):
        raise ValueError("same-level comment identities are duplicated")
    top_level = {
        comment.comment_id: comment
        for comment in comments
        if not comment.parent_comment_id
    }
    for comment in comments:
        if comment.parent_comment_id and (
            comment.parent_comment_id not in top_level
        ):
            raise ValueError("a comment has an invalid or missing exact parent ID")
    for comment in comments:
        visited: set[str] = set()
        current = comment
        while current.parent_comment_id:
            if current.identity_key in visited:
                raise ValueError("comment parent relationships contain a cycle")
            visited.add(current.identity_key)
            current = top_level[current.parent_comment_id]

    return {
        "account_uin": account_uin,
        "post_id": post_id,
        "post_author_uin": post_author_uin,
        "post_content": post_content,
        "declared_comment_count": declared_count,
        "comments": tuple(sorted(comments, key=lambda item: (item.parent_comment_id, item.comment_id))),
    }


def _one_alias(row: Mapping[str, object], aliases: tuple[str, ...], label: str) -> object:
    found = [row[key] for key in aliases if key in row]
    if not found or any(value != found[0] for value in found[1:]):
        raise ValueError(f"{label} is missing or ambiguous")
    return found[0]


def _one_optional_alias(
    row: Mapping[str, object], aliases: tuple[str, ...], label: str
) -> object:
    found = [row[key] for key in aliases if key in row]
    if not found:
        return ""
    if any(value != found[0] for value in found[1:]):
        raise ValueError(f"{label} is ambiguous")
    if found[0] in (None, "", 0):
        return ""
    return found[0]


@dataclass(frozen=True)
class ReplyDraft:
    draft_id: str
    bot_account_uin: str
    owner_actor_uin: str
    owner_umo: str
    post_id: str
    comment_id: str
    target_uin: str
    thread_fingerprint: str
    idempotency_key: str
    content: str
    confirmation_token: str
    status: str = "pending"
    invalidation_reason: str = ""


class ReplyDraftInvalidated(ValueError):
    """A reviewed snapshot changed or could no longer be verified."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CommentReplyDraftStore:
    """Atomic local storage for pending reply drafts and their outcomes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("reply draft store is unreadable") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 2
            or not isinstance(payload.get("drafts"), dict)
        ):
            raise RuntimeError("reply draft store has an invalid shape")
        return payload["drafts"]

    def _write(self, drafts: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 2, "drafts": drafts}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def put(self, draft: ReplyDraft) -> None:
        with self.lock:
            drafts = self._read()
            if draft.draft_id in drafts:
                raise RuntimeError("reply draft ID already exists")
            drafts[draft.draft_id] = asdict(draft)
            self._write(drafts)

    def get(self, draft_id: str) -> ReplyDraft | None:
        with self.lock:
            record = self._read().get(draft_id)
            return ReplyDraft(**record) if record else None

    def finish(self, draft_id: str, status: str) -> ReplyDraft:
        if status not in {"sent", "delivery_unknown"}:
            raise ValueError("reply draft outcome is invalid")
        with self.lock:
            drafts = self._read()
            record = drafts.get(draft_id)
            if not record or record.get("status") != "sending":
                raise RuntimeError("reply draft has no active send claim")
            record["status"] = status
            self._write(drafts)
            return ReplyDraft(**record)

    def claim_send(
        self, draft: ReplyDraft, target: ExactReplyTarget, receipts: CommentReplyReceipts
    ) -> None:
        """Claim an unchanged pending draft and durable receipt under one store lock.

        Args:
            draft: Exact reviewed draft captured before the confirmation re-read.
            target: Verified target derived from that same reviewed draft.
            receipts: Shared permanent exact-target send receipt store.

        Raises:
            ValueError: If the draft changed, was invalidated or was already sent.
            RuntimeError: If durable storage cannot safely persist the claim.
        """
        with self.lock:
            drafts = self._read()
            record = drafts.get(draft.draft_id)
            if not record or record.get("status") != "pending" or ReplyDraft(**record) != draft:
                raise ValueError("reply draft is unavailable or changed before send claim")
            if not receipts.claim(target):
                raise ValueError("reply target already has a send receipt")
            record["status"] = "sending"
            self._write(drafts)

    def invalidate(self, draft_id: str, reason: str) -> None:
        """Permanently invalidate a pending draft before any send claim.

        Args:
            draft_id: Previously reviewed local draft identifier.
            reason: Safe reason code, without transport or credential details.

        Raises:
            RuntimeError: If the local store cannot be safely read or written.
        """
        with self.lock:
            drafts = self._read()
            record = drafts.get(draft_id)
            if record and record.get("status") == "pending":
                record["status"] = "invalidated"
                record["invalidation_reason"] = reason
                self._write(drafts)


class CommentReplyWorkflow:
    """Create owner-scoped drafts and send only after exact confirmation."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        draft_store: CommentReplyDraftStore,
        receipts: CommentReplyReceipts,
        thread_loader: Callable[[str, str], Awaitable[Mapping[str, object]]],
        draft_generator: Callable[[VerifiedComment, str], Awaitable[str]],
        sender: Callable[[ExactReplyTarget, str], Awaitable[object]],
        on_sent: Callable[[ExactReplyTarget, str, object], Awaitable[None]] | None = None,
    ):
        self.enabled = enabled is True
        self.draft_store = draft_store
        self.receipts = receipts
        self.thread_loader = thread_loader
        self.draft_generator = draft_generator
        self.sender = sender
        self.on_sent = on_sent

    @staticmethod
    def _check_owner(
        *,
        bot_account_uin: str,
        owner_actor_uin: str,
        owner_umo: str,
        active_uin: str,
        active_umo: str,
        is_private: bool,
        is_admin: bool,
    ) -> None:
        if (
            not _UIN.fullmatch(bot_account_uin)
            or not _UIN.fullmatch(owner_actor_uin)
            or bot_account_uin == owner_actor_uin
            or active_uin != owner_actor_uin
            or not owner_umo
            or owner_umo != f"qq_napcat_{bot_account_uin}:FriendMessage:{owner_actor_uin}"
            or active_umo != owner_umo
            or is_private is not True
            or is_admin is not True
        ):
            raise PermissionError("owner-only private session required")

    async def prepare_reply(
        self,
        *,
        bot_account_uin: str,
        owner_actor_uin: str,
        owner_umo: str,
        active_uin: str,
        active_umo: str,
        is_private: bool,
        is_admin: bool,
        post_id: str,
        comment_id: str,
        expected_target_uin: str,
    ) -> ReplyDraft:
        if not self.enabled:
            raise RuntimeError("qzone_comment_reply_disabled")
        self._check_owner(
            bot_account_uin=bot_account_uin,
            owner_actor_uin=owner_actor_uin,
            owner_umo=owner_umo,
            active_uin=active_uin,
            active_umo=active_umo,
            is_private=is_private,
            is_admin=is_admin,
        )
        if _uin(expected_target_uin, "expected target UIN") != owner_actor_uin:
            raise PermissionError("only the authorized owner's comments may be replied to")
        snapshot = normalize_comment_thread(
            await self.thread_loader(bot_account_uin, post_id)
        )
        if snapshot["account_uin"] != bot_account_uin or snapshot["post_id"] != normalize_post_id(post_id):
            raise PermissionError("the loaded account or post does not match the request")
        exact_comment_id = _identifier(comment_id, "comment ID")
        target = bind_reply_target(
            account_uin=bot_account_uin,
            post_id=snapshot["post_id"],
            post_author_uin=snapshot["post_author_uin"],
            post_content=snapshot["post_content"],
            comment_id=exact_comment_id,
            expected_target_uin=_uin(expected_target_uin, "expected target UIN"),
            declared_comment_count=snapshot["declared_comment_count"],
            comments=snapshot["comments"],
        )
        comment = next(item for item in snapshot["comments"] if item.comment_id == exact_comment_id)
        content = await self.draft_generator(comment, snapshot["post_content"])
        if not isinstance(content, str) or not content.strip() or len(content) > 1000:
            raise ValueError("generated reply draft is empty or too long")
        content = content.strip()
        draft_id = uuid.uuid4().hex
        token = hashlib.sha256(
            f"{target.idempotency_key}\0{draft_id}\0{content}".encode()
        ).hexdigest()
        draft = ReplyDraft(
            draft_id=draft_id,
            bot_account_uin=bot_account_uin,
            owner_actor_uin=owner_actor_uin,
            owner_umo=owner_umo,
            post_id=target.post_id,
            comment_id=target.parent_comment_id,
            target_uin=target.target_uin,
            thread_fingerprint=target.thread_fingerprint,
            idempotency_key=target.idempotency_key,
            content=content,
            confirmation_token=token,
        )
        self.draft_store.put(draft)
        return draft

    async def send_confirmed(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        bot_account_uin: str,
        owner_actor_uin: str,
        owner_umo: str,
        active_uin: str,
        active_umo: str,
        is_private: bool,
        is_admin: bool,
    ) -> ReplyDraft:
        if not self.enabled:
            raise RuntimeError("qzone_comment_reply_disabled")
        self._check_owner(
            bot_account_uin=bot_account_uin,
            owner_actor_uin=owner_actor_uin,
            owner_umo=owner_umo,
            active_uin=active_uin,
            active_umo=active_umo,
            is_private=is_private,
            is_admin=is_admin,
        )
        draft = self.draft_store.get(draft_id)
        if draft is None or draft.status != "pending":
            raise ValueError("reply draft is unavailable")
        if (
            draft.bot_account_uin != bot_account_uin or draft.owner_actor_uin != owner_actor_uin
            or draft.owner_umo != owner_umo or draft.target_uin != owner_actor_uin
        ):
            raise PermissionError("reply draft owner scope mismatch")
        if not hmac.compare_digest(draft.confirmation_token, confirmation_token):
            raise PermissionError("reply draft confirmation token mismatch")
        # Re-read the exact post/thread at confirmation time. If its content,
        # IDs, authors, or reply structure changed since drafting, stop before
        # claiming a receipt or invoking the external sender.
        try:
            snapshot = normalize_comment_thread(
                await self.thread_loader(bot_account_uin, draft.post_id)
            )
            if snapshot["account_uin"] != draft.bot_account_uin or snapshot["post_id"] != draft.post_id:
                raise ValueError("the loaded account or post no longer matches the draft")
            current_target = bind_reply_target(
                account_uin=bot_account_uin,
                post_id=snapshot["post_id"],
                post_author_uin=snapshot["post_author_uin"],
                post_content=snapshot["post_content"],
                comment_id=draft.comment_id,
                expected_target_uin=draft.target_uin,
                declared_comment_count=snapshot["declared_comment_count"],
                comments=snapshot["comments"],
            )
        except Exception as exc:
            reason = (
                "comment_thread_completeness_unknown"
                if getattr(exc, "code", None) == "comment_thread_completeness_unknown"
                else "thread_verification_failed"
            )
            self.draft_store.invalidate(draft_id, reason)
            raise ReplyDraftInvalidated(reason) from None
        if (
            current_target.thread_fingerprint != draft.thread_fingerprint
            or current_target.idempotency_key != draft.idempotency_key
        ):
            self.draft_store.invalidate(draft_id, "thread_changed")
            raise ReplyDraftInvalidated("thread_changed")
        target = ExactReplyTarget(
            account_uin=draft.bot_account_uin,
            post_id=draft.post_id,
            parent_comment_id=draft.comment_id,
            target_uin=draft.target_uin,
            thread_fingerprint=draft.thread_fingerprint,
            idempotency_key=draft.idempotency_key,
            parent_identity_key=current_target.parent_identity_key,
            legacy_idempotency_key=current_target.legacy_idempotency_key,
        )
        # Recheck pending state and persist both claims before the external call.
        # An overlapping confirmation cannot send a draft invalidated meanwhile.
        self.draft_store.claim_send(draft, target, self.receipts)
        try:
            result = await self.sender(target, draft.content)
        except asyncio.CancelledError:
            self.receipts.finish(target, status="delivery_unknown")
            return self.draft_store.finish(draft_id, "delivery_unknown")
        except Exception:
            self.receipts.finish(target, status="delivery_unknown")
            return self.draft_store.finish(draft_id, "delivery_unknown")
        status = "sent" if _community_success(result) else "delivery_unknown"
        self.receipts.finish(target, status=status)
        finished = self.draft_store.finish(draft_id, status)
        if status == "sent" and self.on_sent is not None:
            try:
                await self.on_sent(target, draft.content, result)
            except (Exception, asyncio.CancelledError):
                # Local sync cannot change an actual send or permit a resend.
                pass
        return finished


def _community_success(result: object) -> bool:
    return (
        isinstance(result, Mapping)
        and type(result.get("code")) is int
        and result["code"] == 0
        and type(result.get("ret")) is int
        and result["ret"] == 0
    )
