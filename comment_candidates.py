"""Offline candidates for bounded Qzone subreply reads and new-root replies.

This module is intentionally not imported by the production plugin entrypoint.
It provides injectable readers and senders for fixture tests only.  A future
integration must keep the feature disabled until an approved account sample
establishes the endpoint-specific response contract.
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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .comment_reply import ExactReplyTarget, comment_identity_key, normalize_post_id

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_UIN = re.compile(r"[0-9]{5,12}\Z")


class CandidateReadError(ValueError):
    """A fixture or endpoint response cannot establish the requested evidence."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _id(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CandidateReadError(f"{field}_invalid")
    result = str(value).strip()
    if not _ID.fullmatch(result):
        raise CandidateReadError(f"{field}_invalid")
    return result


def _uin(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CandidateReadError(f"{field}_invalid")
    result = str(value).strip()
    if not _UIN.fullmatch(result):
        raise CandidateReadError(f"{field}_invalid")
    return result


def _required_alias(row: Mapping[str, object], aliases: tuple[str, ...], field: str) -> object:
    """Read one required field and reject conflicting wire aliases.

    Args:
        row: One decoded response row.
        aliases: Equivalent wire names for the field.
        field: Error-code prefix for malformed or conflicting values.

    Returns:
        The single agreed value.

    Raises:
        CandidateReadError: If the aliases are missing or disagree.
    """
    values = [row[key] for key in aliases if key in row]
    if not values or any(value != values[0] for value in values[1:]):
        raise CandidateReadError(f"{field}_ambiguous")
    return values[0]


def _parent_alias(row: Mapping[str, object]) -> object | None:
    """Read a parent ID only when the response proves one exists.

    Args:
        row: One decoded comment row.

    Returns:
        The exact parent ID, or ``None`` when no parent field is supplied.

    Raises:
        CandidateReadError: If aliases conflict or an explicit empty parent is supplied.
    """
    aliases = ("parent_comment_id", "parentCommentId", "replyid", "reply_comment_id")
    values = [row[key] for key in aliases if key in row]
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise CandidateReadError("reply_parent_ambiguous")
    if values[0] in (None, "", 0):
        raise CandidateReadError("reply_parent_missing")
    return values[0]


def _mapping_value(payload: Mapping[str, object], key: str) -> object:
    nested = payload.get("data")
    if key in payload:
        value = payload[key]
        if isinstance(nested, Mapping) and key in nested and nested[key] != value:
            raise CandidateReadError("reply_response_shape_invalid")
        return value
    if isinstance(nested, Mapping) and key in nested:
        return nested[key]
    return None


def _page_bool(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise CandidateReadError("reply_pagination_marker_invalid")


def _optional_cursor(payload: Mapping[str, object]) -> str | None:
    values: list[object] = []
    for key in (
        "next_cursor",
        "nextCursor",
        "next_pos",
        "nextPos",
        "next_start",
        "nextStart",
    ):
        value = _mapping_value(payload, key)
        if value is not None:
            values.append(value)
    if not values:
        return None
    normalized = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise CandidateReadError("reply_pagination_cursor_invalid")
        normalized.append(str(value).strip())
    if any(value != normalized[0] for value in normalized[1:]):
        raise CandidateReadError("reply_pagination_cursor_conflict")
    return normalized[0] or None


@dataclass(frozen=True)
class SubreplyPage:
    """One parsed child-comment page and its endpoint markers."""

    parent_comment_id: str
    replies: tuple[dict[str, object], ...]
    has_more: bool | None
    next_cursor: str | None
    reply_count_hint: int | None


def parse_subreply_page(
    payload: Mapping[str, object],
    *,
    parent_comment_id: str,
) -> SubreplyPage:
    """Parse one community-shaped child-comment response without networking.

    Args:
        payload: Decoded JSON response from a fixture or injected transport.
        parent_comment_id: Exact root-comment ID requested by the caller.

    Returns:
        Parsed replies and the response's explicit pagination evidence.

    Raises:
        CandidateReadError: If IDs, parent relationships, or markers are unsafe.
    """
    if not isinstance(payload, Mapping):
        raise CandidateReadError("reply_response_invalid")
    status_values = [payload[key] for key in ("code", "ret") if key in payload]
    if status_values and any(
        type(value) is not int or value != 0 for value in status_values
    ):
        raise CandidateReadError("reply_api_error")
    rows = _mapping_value(payload, "comments")
    if not isinstance(rows, list):
        raise CandidateReadError("reply_list_missing")
    parent = _id(parent_comment_id, "parent_comment_id")
    replies: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise CandidateReadError("reply_row_invalid")
        reply_id = _id(_required_alias(row, ("tid", "commentid"), "reply_id"), "reply_id")
        author = _uin(_required_alias(row, ("uin", "author_uin"), "reply_author"), "reply_author")
        content = _required_alias(row, ("content", "text"), "reply_content")
        if not isinstance(content, str):
            raise CandidateReadError("reply_content_invalid")
        supplied_parent = _parent_alias(row)
        if supplied_parent is not None and _id(supplied_parent, "reply_parent_id") != parent:
            raise CandidateReadError("reply_parent_mismatch")
        identity = comment_identity_key(reply_id, parent)
        if identity in seen:
            raise CandidateReadError("reply_identity_duplicate")
        seen.add(identity)
        replies.append(
            {
                "commentid": reply_id,
                "comment_identity_key": identity,
                "comment_level": "reply",
                "uin": author,
                "content": content,
                "parent_comment_id": parent,
            }
        )

    has_more = _page_bool(
        next(
            (
                payload[key]
                for key in ("has_more", "hasMore", "has_more_comments")
                if key in payload
            ),
            next(
                (
                    nested[key]
                    for nested in [payload.get("data")]
                    if isinstance(nested, Mapping)
                    for key in ("has_more", "hasMore", "has_more_comments")
                    if key in nested
                ),
                None,
            ),
        )
    )
    next_cursor = _optional_cursor(payload)
    reply_count_hint = _mapping_value(payload, "reply_num")
    if reply_count_hint is None:
        reply_count_hint = _mapping_value(payload, "replyNum")
    if reply_count_hint is not None:
        if isinstance(reply_count_hint, bool) or not isinstance(
            reply_count_hint, int | str
        ):
            raise CandidateReadError("reply_count_hint_invalid")
        try:
            reply_count_hint = int(str(reply_count_hint).strip())
        except ValueError:
            raise CandidateReadError("reply_count_hint_invalid") from None
        if reply_count_hint < len(replies):
            raise CandidateReadError("reply_count_hint_inconsistent")
    return SubreplyPage(parent, tuple(replies), has_more, next_cursor, reply_count_hint)


async def read_subreply_pages(
    fetch_page: Callable[[str | None], Awaitable[Mapping[str, object]]],
    *,
    parent_comment_id: str,
    page_size: int = 20,
    max_pages: int = 32,
) -> dict[str, object]:
    """Read child pages only when the route supplies an explicit terminator.

    Args:
        fetch_page: Injected function receiving the next cursor; it must not
            perform network I/O in fixture tests.
        parent_comment_id: Root-comment ID whose children are requested.
        page_size: Requested page size used only for a diagnostic result.
        max_pages: Hard safety bound against a broken cursor loop.

    Returns:
        Replies keyed by their hierarchical identity and endpoint evidence.

    Raises:
        CandidateReadError: If pagination cannot prove termination or is unsafe.
    """
    if not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page_size must be positive")
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be positive")
    parent = _id(parent_comment_id, "parent_comment_id")
    cursor: str | None = None
    seen_cursors: set[str | None] = set()
    replies: dict[str, dict[str, object]] = {}
    hints: set[int] = set()
    for _ in range(max_pages):
        if cursor in seen_cursors:
            raise CandidateReadError("reply_pagination_cursor_loop")
        seen_cursors.add(cursor)
        page = parse_subreply_page(await fetch_page(cursor), parent_comment_id=parent)
        for reply in page.replies:
            identity = str(reply["comment_identity_key"])
            if identity in replies:
                raise CandidateReadError("reply_identity_duplicate")
            replies[identity] = reply
        if page.reply_count_hint is not None:
            hints.add(page.reply_count_hint)
        if page.has_more is False:
            if page.next_cursor is not None:
                raise CandidateReadError("reply_pagination_marker_conflict")
            if hints and any(value != len(replies) for value in hints):
                raise CandidateReadError("reply_count_hint_inconsistent")
            return {
                "parent_comment_id": parent,
                "replies": tuple(replies.values()),
                "pages": len(seen_cursors),
                "termination": "explicit_has_more_false",
                "reply_count_hints": tuple(sorted(hints)),
                "requested_page_size": page_size,
            }
        if page.has_more is True:
            if page.next_cursor is None or page.next_cursor in seen_cursors:
                raise CandidateReadError("reply_pagination_cursor_missing_or_repeated")
            cursor = page.next_cursor
            continue
        raise CandidateReadError("reply_pagination_termination_unknown")
    raise CandidateReadError("reply_pagination_over_limit")


def inspect_embedded_replies(root: Mapping[str, object]) -> dict[str, object]:
    """Report what an embedded list_3 can prove without treating it as complete."""
    if not isinstance(root, Mapping):
        raise CandidateReadError("root_comment_invalid")
    reply_num = root.get("reply_num", root.get("replyNum"))
    if isinstance(reply_num, bool) or not isinstance(reply_num, int | str):
        raise CandidateReadError("reply_count_hint_invalid")
    try:
        reply_num = int(str(reply_num).strip())
    except ValueError:
        raise CandidateReadError("reply_count_hint_invalid") from None
    rows = root.get("list_3", [])
    if not isinstance(rows, list):
        raise CandidateReadError("embedded_reply_list_invalid")
    return {
        "reply_num": reply_num,
        "embedded_count": len(rows),
        "count_hint_matches": reply_num == len(rows),
        "completeness": "unknown",
        "next_action": "use_endpoint_with_explicit_termination"
        if reply_num
        else "none_observed",
    }


@dataclass(frozen=True)
class FutureTopLevelComment:
    """Exact post/root/comment context needed by the future-root candidate."""

    account_uin: str
    post_id: str
    post_author_uin: str
    post_content: str
    comment_id: str
    comment_author_uin: str
    comment_content: str


def normalize_future_top_level_snapshot(
    snapshot: Mapping[str, object],
    *,
    account_uin: str,
    expected_post_id: str,
    expected_comment_id: str,
    expected_author_uin: str,
) -> FutureTopLevelComment:
    """Validate one new top-level comment without loading historical replies.

    Args:
        snapshot: Adapter-normalized post and one exact root comment.
        account_uin: Account that owns the post and executes the reply.
        expected_post_id: Post ID selected by the caller.
        expected_comment_id: Root comment ID selected by the caller.
        expected_author_uin: Required author of the root comment.

    Returns:
        Exact post and root comment identity with their current text.

    Raises:
        CandidateReadError: If the post, root, author, or hierarchy is ambiguous.
    """
    account = _uin(account_uin, "account_uin")
    post_id = normalize_post_id(expected_post_id)
    comment_id = _id(expected_comment_id, "comment_id")
    author = _uin(expected_author_uin, "comment_author")
    if not isinstance(snapshot, Mapping):
        raise CandidateReadError("future_root_snapshot_invalid")
    if _uin(snapshot.get("account_uin"), "account_uin") != account:
        raise CandidateReadError("future_root_account_mismatch")
    if normalize_post_id(snapshot.get("post_id")) != post_id:
        raise CandidateReadError("future_root_post_mismatch")
    if _uin(snapshot.get("post_author_uin"), "post_author_uin") != account:
        raise CandidateReadError("future_root_post_author_mismatch")
    post_content = snapshot.get("post_content")
    if not isinstance(post_content, str) or not post_content.strip():
        raise CandidateReadError("future_root_post_content_missing")
    row = snapshot.get("comment")
    if not isinstance(row, Mapping):
        raise CandidateReadError("future_root_comment_missing")
    if _id(_required_alias(row, ("tid", "commentid"), "comment_id"), "comment_id") != comment_id:
        raise CandidateReadError("future_root_comment_mismatch")
    if _uin(_required_alias(row, ("uin", "author_uin"), "comment_author"), "comment_author") != author:
        raise CandidateReadError("future_root_author_mismatch")
    if _parent_alias(row) is not None:
        raise CandidateReadError("future_root_not_top_level")
    if row.get("comment_level") not in (None, "top_level"):
        raise CandidateReadError("future_root_not_top_level")
    content = _required_alias(row, ("content", "text"), "future_root_comment_content")
    if not isinstance(content, str) or not content.strip():
        raise CandidateReadError("future_root_comment_content_missing")
    return FutureTopLevelComment(
        account, post_id, account, post_content, comment_id, author, content
    )


def _future_root_target(comment: FutureTopLevelComment) -> ExactReplyTarget:
    canonical = {
        "schema_version": 1,
        "account_uin": comment.account_uin,
        "post_id": comment.post_id,
        "post_author_uin": comment.post_author_uin,
        "post_content": comment.post_content,
        "comment_id": comment.comment_id,
        "comment_author_uin": comment.comment_author_uin,
        "comment_content": comment.comment_content,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    identity = comment_identity_key(comment.comment_id)
    idempotency_key = hashlib.sha256(
        f"future-top-level-v1\0{comment.account_uin}\0{comment.post_id}\0{identity}\0{comment.comment_author_uin}".encode()
    ).hexdigest()
    return ExactReplyTarget(
        account_uin=comment.account_uin,
        post_id=comment.post_id,
        parent_comment_id=comment.comment_id,
        target_uin=comment.comment_author_uin,
        thread_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        parent_identity_key=identity,
    )


@dataclass(frozen=True)
class FutureTopLevelReplyDraft:
    """Persisted candidate draft bound to a baseline generation and root."""

    draft_id: str
    account_uin: str
    baseline_generation: int
    post_id: str
    comment_id: str
    target_uin: str
    target_fingerprint: str
    idempotency_key: str
    content: str
    confirmation_token: str
    status: str = "pending"
    invalidation_reason: str = ""


class FutureTopLevelStateStore:
    """Persist baseline IDs, drafts, and one-shot receipts in one local file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "schema_version": 2,
                "enabled": False,
                "generation": 0,
                "account_uin": "",
                "post_author_uin": "",
                "baseline": {},
                "drafts": {},
                "receipts": {},
            }
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("future-root state is unreadable") from exc
        if not isinstance(value, dict):
            raise RuntimeError("future-root state has an invalid shape")
        if value.get("schema_version") == 1:
            raise RuntimeError("future-root state requires account-bound migration")
        if (
            value.get("schema_version") != 2
            or not isinstance(value.get("account_uin"), str)
            or not isinstance(value.get("post_author_uin"), str)
            or not isinstance(value.get("baseline"), dict)
            or not isinstance(value.get("drafts"), dict)
            or not isinstance(value.get("receipts"), dict)
        ):
            raise RuntimeError("future-root state has an invalid shape")
        return value

    def _write(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def enable_baseline(
        self,
        post_comment_ids: Mapping[str, Iterable[str]],
        *,
        account_uin: str,
        post_author_uin: str,
        complete: bool,
    ) -> int:
        """Enable the candidate only after a complete top-level ID baseline.

        Args:
            post_comment_ids: Every known top-level comment ID for each covered post.
            account_uin: Execution account bound to the baseline.
            post_author_uin: Post author bound to the baseline.
            complete: Explicit caller attestation that the top-level read is complete.

        Returns:
            New baseline generation number.

        Raises:
            ValueError: If the identities, baseline, or completeness attestation is invalid.
        """
        account = _uin(account_uin, "account_uin")
        post_author = _uin(post_author_uin, "post_author_uin")
        if complete is not True:
            raise ValueError("a complete top-level baseline is required")
        if not isinstance(post_comment_ids, Mapping) or not post_comment_ids:
            raise ValueError("a non-empty top-level baseline is required")
        baseline: dict[str, list[str]] = {}
        for post_id, comment_ids in post_comment_ids.items():
            normalized_post = normalize_post_id(post_id)
            if isinstance(comment_ids, (str, bytes)):
                raise ValueError("baseline comment IDs must be an iterable of IDs")
            try:
                raw_ids = list(comment_ids)
            except TypeError:
                raise ValueError("baseline comment IDs must be an iterable of IDs") from None
            normalized_values = [_id(value, "baseline_comment_id") for value in raw_ids]
            if len(set(normalized_values)) != len(normalized_values):
                raise ValueError("baseline comment IDs are duplicated")
            normalized_ids = sorted(normalized_values)
            baseline[normalized_post] = normalized_ids
        with self.lock:
            value = self._read()
            generation = int(value.get("generation", 0)) + 1
            value.update(
                enabled=True,
                generation=generation,
                account_uin=account,
                post_author_uin=post_author,
                baseline=baseline,
            )
            self._write(value)
            return generation

    def baseline_for(
        self,
        post_id: str,
        *,
        account_uin: str,
        post_author_uin: str,
    ) -> tuple[bool, int, frozenset[str], bool]:
        """Return enable state, generation, IDs, and whether this post is covered.

        Args:
            post_id: Exact post identifier to look up.
            account_uin: Execution account bound to the baseline.
            post_author_uin: Post author bound to the baseline.

        Returns:
            Enable state, generation, known IDs, and post coverage.

        Raises:
            RuntimeError: If the state is legacy, malformed, or bound to another identity.
            CandidateReadError: If an identity is malformed.
        """
        normalized_post = normalize_post_id(post_id)
        account = _uin(account_uin, "account_uin")
        post_author = _uin(post_author_uin, "post_author_uin")
        with self.lock:
            value = self._read()
            if value.get("enabled") is True and (
                value.get("account_uin") != account
                or value.get("post_author_uin") != post_author
            ):
                raise RuntimeError("future-root baseline account binding mismatch")
            present = normalized_post in value["baseline"]
            baseline = value["baseline"].get(normalized_post, [])
            if not isinstance(baseline, list) or any(
                not isinstance(item, str) for item in baseline
            ):
                raise RuntimeError("future-root baseline is malformed")
            return (
                bool(value.get("enabled") is True),
                int(value.get("generation", 0)),
                frozenset(baseline),
                present,
            )

    def put_draft(self, draft: FutureTopLevelReplyDraft) -> None:
        with self.lock:
            value = self._read()
            drafts = value["drafts"]
            if draft.draft_id in drafts:
                raise RuntimeError("future-root draft ID already exists")
            drafts[draft.draft_id] = asdict(draft)
            self._write(value)

    def get_draft(self, draft_id: str) -> FutureTopLevelReplyDraft | None:
        with self.lock:
            record = self._read()["drafts"].get(draft_id)
            return FutureTopLevelReplyDraft(**record) if record else None

    def invalidate(self, draft_id: str, reason: str) -> None:
        with self.lock:
            value = self._read()
            record = value["drafts"].get(draft_id)
            if record and record.get("status") == "pending":
                record["status"] = "invalidated"
                record["invalidation_reason"] = reason
                self._write(value)

    def claim(self, draft: FutureTopLevelReplyDraft, target: ExactReplyTarget) -> None:
        """Atomically claim the draft and exact idempotency key before sending."""
        with self.lock:
            value = self._read()
            record = value["drafts"].get(draft.draft_id)
            if (
                not record
                or record.get("status") != "pending"
                or FutureTopLevelReplyDraft(**record) != draft
            ):
                raise ValueError("future-root draft is unavailable or changed")
            receipts = value["receipts"]
            if target.idempotency_key in receipts:
                raise ValueError("future-root target already has a receipt")
            receipts[target.idempotency_key] = {"status": "sending", **asdict(target)}
            record["status"] = "sending"
            self._write(value)

    def finish(
        self, draft_id: str, target: ExactReplyTarget, status: str
    ) -> FutureTopLevelReplyDraft:
        """Persist a terminal outcome; neither success nor uncertainty retries."""
        if status not in {"sent", "delivery_unknown"}:
            raise ValueError("future-root outcome is invalid")
        with self.lock:
            value = self._read()
            record = value["drafts"].get(draft_id)
            receipt = value["receipts"].get(target.idempotency_key)
            if not record or record.get("status") != "sending" or not receipt:
                raise RuntimeError("future-root send claim is missing")
            record["status"] = status
            receipt["status"] = status
            self._write(value)
            return FutureTopLevelReplyDraft(**record)


class FutureTopLevelReplyWorkflow:
    """Prepare one baseline-new root reply with an injectable final reread."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        dry_run: bool = True,
        state: FutureTopLevelStateStore,
        snapshot_loader: Callable[[str, str, str], Awaitable[Mapping[str, object]]],
        draft_generator: Callable[[FutureTopLevelComment], Awaitable[str]],
        sender: Callable[[ExactReplyTarget, str], Awaitable[object]],
    ):
        self.enabled = enabled is True
        self.dry_run = dry_run is True
        self.state = state
        self.snapshot_loader = snapshot_loader
        self.draft_generator = draft_generator
        self.sender = sender

    async def prepare_reply(
        self,
        snapshot: Mapping[str, object],
        *,
        account_uin: str,
        post_id: str,
        comment_id: str,
        owner_actor_uin: str,
    ) -> FutureTopLevelReplyDraft:
        """Create a draft only for a root absent from the enable baseline."""
        if not self.enabled:
            raise RuntimeError("future_top_level_reply_disabled")
        comment = normalize_future_top_level_snapshot(
            snapshot,
            account_uin=account_uin,
            expected_post_id=post_id,
            expected_comment_id=comment_id,
            expected_author_uin=owner_actor_uin,
        )
        enabled, generation, known_ids, covered = self.state.baseline_for(
            comment.post_id,
            account_uin=comment.account_uin,
            post_author_uin=comment.post_author_uin,
        )
        if not enabled or not covered:
            raise RuntimeError("future_top_level_baseline_disabled")
        if comment.comment_id in known_ids:
            raise CandidateReadError("future_root_is_before_enable_baseline")
        target = _future_root_target(comment)
        content = await self.draft_generator(comment)
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content.strip()) > 1000
        ):
            raise ValueError("future-root reply draft is empty or too long")
        content = content.strip()
        draft_id = uuid.uuid4().hex
        token = hashlib.sha256(
            f"{target.idempotency_key}\0{draft_id}\0{content}".encode()
        ).hexdigest()
        draft = FutureTopLevelReplyDraft(
            draft_id=draft_id,
            account_uin=comment.account_uin,
            baseline_generation=generation,
            post_id=comment.post_id,
            comment_id=comment.comment_id,
            target_uin=comment.comment_author_uin,
            target_fingerprint=target.thread_fingerprint,
            idempotency_key=target.idempotency_key,
            content=content,
            confirmation_token=token,
        )
        self.state.put_draft(draft)
        return draft

    async def send_confirmed(
        self, draft_id: str, confirmation_token: str
    ) -> FutureTopLevelReplyDraft:
        """Reread the exact root, claim once, and optionally invoke the sender."""
        if not self.enabled:
            raise RuntimeError("future_top_level_reply_disabled")
        if self.dry_run:
            raise RuntimeError("future_top_level_reply_dry_run")
        draft = self.state.get_draft(draft_id)
        if draft is None or draft.status != "pending":
            raise ValueError("future-root draft is unavailable")
        if not hmac.compare_digest(draft.confirmation_token, confirmation_token):
            raise PermissionError("future-root confirmation token mismatch")
        enabled, generation, known_ids, covered = self.state.baseline_for(
            draft.post_id,
            account_uin=draft.account_uin,
            post_author_uin=draft.account_uin,
        )
        if (
            not enabled
            or not covered
            or generation != draft.baseline_generation
            or draft.comment_id in known_ids
        ):
            self.state.invalidate(draft_id, "baseline_changed")
            raise CandidateReadError("future_root_baseline_changed")
        try:
            current = normalize_future_top_level_snapshot(
                await self.snapshot_loader(
                    draft.post_id, draft.comment_id, draft.target_uin
                ),
                account_uin=draft.account_uin,
                expected_post_id=draft.post_id,
                expected_comment_id=draft.comment_id,
                expected_author_uin=draft.target_uin,
            )
        except Exception:
            self.state.invalidate(draft_id, "root_reread_failed")
            raise CandidateReadError("future_root_reread_failed") from None
        target = _future_root_target(current)
        if (
            target.thread_fingerprint != draft.target_fingerprint
            or target.idempotency_key != draft.idempotency_key
        ):
            self.state.invalidate(draft_id, "root_changed")
            raise CandidateReadError("future_root_changed")
        self.state.claim(draft, target)
        try:
            result = await self.sender(target, draft.content)
        except asyncio.CancelledError:
            self.state.finish(draft_id, target, "delivery_unknown")
            raise
        except Exception:
            self.state.finish(draft_id, target, "delivery_unknown")
            finished = self.state.get_draft(draft_id)
            if finished is None:
                raise RuntimeError("future-root draft disappeared after send failure")
            return finished
        status = (
            "sent"
            if isinstance(result, Mapping)
            and type(result.get("code")) is int
            and result["code"] == 0
            and type(result.get("ret")) is int
            and result["ret"] == 0
            else "delivery_unknown"
        )
        return self.state.finish(draft_id, target, status)
