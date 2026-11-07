"""Durable local projections of already authorized Qzone observations."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .comment_reply import matches_reply_content

# 2026-10-17：公开仓库只保留占位符。真实账号放在 **不入库** 的 local_deployment.py
# （与 main.py 同一套机制）；缺失时回退占位符（= 同步闸门全关，不会猜号）。
try:  # pragma: no cover - 部署相关，跑测试走回退分支
    from .local_deployment import (  # type: ignore[attr-defined]
        COMMENT_BOT_ACCOUNT_UIN,
        COMMENT_OWNER_ACTOR_UIN,
    )

    OWNER_ID = COMMENT_OWNER_ACTOR_UIN
    ASSISTANT_ID = COMMENT_BOT_ACCOUNT_UIN
except ImportError:
    OWNER_ID = "100000001"
    ASSISTANT_ID = "100000002"


class MemorySyncBlocked(ValueError):
    """The source or existing memory requires explicit review."""


def _bucket_id(kind: str, remote_id: str, *, source_key: str = "") -> str:
    """Keep the existing deterministic publication identity.

    Args:
        kind: Source kind.
        remote_id: Exact remote source ID.
        source_key: Additional source scope.

    Returns:
        Stable memory ID.
    """
    return (
        "qz_"
        + hashlib.sha256(
            f"qzone\0{kind}\0{source_key}\0{remote_id}".encode()
        ).hexdigest()
    )


async def sync_published_post(context: Any, draft: Any, *, owner_id: str) -> str | None:
    """Project an actually published draft without generating or sending.

    Args:
        context: Existing AstrBot registry.
        draft: Publication receipt with status and exact remote tid.
        owner_id: Fixed private owner.

    Returns:
        Stable memory ID, or None for an ineligible source.

    Raises:
        MemorySyncBlocked: If the private owner differs.
    """
    if draft.status != "published" or not draft.remote_tid:
        return None
    if owner_id != OWNER_ID:
        raise MemorySyncBlocked("owner_scope_mismatch")
    payload = {
        "source": "QQ空间",
        "account_id": ASSISTANT_ID,
        "owner_id": OWNER_ID,
        "post_id": draft.remote_tid,
        "memory_id": _bucket_id("post", draft.remote_tid),
        "revision": 1,
        "evidence": "verified_remote_thread",
        "deleted": False,
        "post": {
            "source_id": f"post:{draft.remote_tid}",
            "remote_id": draft.remote_tid,
            "author_kind": "assistant",
            "author_id": ASSISTANT_ID,
            "content": draft.content,
            "parent_id": "",
            "created_at": draft.published_at or "",
            "draft_id": draft.draft_id,
            "source_created_at": draft.created_at,
            "publication_state": "published",
            "publication_evidence": "published_with_remote_tid",
        },
        "comments": [],
        "deleted_source_ids": [],
    }
    getter = getattr(context, "get_registered_star", None)
    metadata = getter("astrbot_plugin_ob_memory") if callable(getter) else None
    plugin = getattr(metadata, "star_cls", None)
    if plugin is None or getattr(metadata, "activated", True) is False:
        return None
    if str(
        getattr(plugin, "config", {}).get("scope_mode", "conversation")
    ).lower() not in {"hybrid", "user"}:
        raise MemorySyncBlocked("private_user_scope_required")
    service = getattr(plugin, "social_sync", None)
    if not callable(getattr(service, "apply_snapshot", None)):
        return None
    try:
        return await service.apply_snapshot(payload)
    except ValueError as exc:
        raise MemorySyncBlocked(str(exc)) from exc


class QzoneMemoryOutbox:
    """Keep filtered receipts without autonomous reads or cached-source replay."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self) -> dict:
        """Read the local outbox, failing closed on invalid data.

        Returns:
            Persistent sequence and owned post snapshots.
        """
        if not self.path.exists():
            return {"schema_version": 1, "sequence": 1, "posts": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            value.get("schema_version") != 1
            or not isinstance(value.get("posts"), dict)
            or type(value.get("sequence")) is not int
        ):
            raise MemorySyncBlocked("comment_memory_outbox_invalid")
        return value

    def _save(self, value: dict) -> None:
        """Atomically replace only the dedicated local outbox.

        Args:
            value: JSON-compatible owned observations.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def begin_observation(self) -> int:
        """Reserve order before the existing authorized read begins.

        Returns:
            Persistent increasing observation sequence.
        """
        with self.lock:
            value = self._load()
            value["sequence"] += 1
            self._save(value)
            return value["sequence"]

    def observe(self, snapshot: dict, revision: int) -> bool:
        """Persist only this owner's interactions from a counted whole thread.

        Args:
            snapshot: Existing workflow's normalized counted remote thread.
            revision: Sequence reserved before its remote read started.

        Returns:
            Whether this observation superseded the stored version.

        Raises:
            MemorySyncBlocked: If scope or reply correspondence is unclear.
        """
        if (
            snapshot["account_uin"] != ASSISTANT_ID
            or snapshot["post_author_uin"] != ASSISTANT_ID
        ):
            raise MemorySyncBlocked("remote_post_scope_mismatch")
        comments = snapshot["comments"]
        if type(snapshot.get("declared_comment_count")) is not int or snapshot[
            "declared_comment_count"
        ] != len(comments):
            raise MemorySyncBlocked("complete_comment_count_required")
        # 2026-10-31：QQ空间楼内回复可能复用主评论的 commentid（例如
        # top_level:1 与 reply:1:1 的 commentid 都是 1）。原先用 comment_id
        # 做选中集合和 source_id，会让根评论与回复互相覆盖/成环，星图同步
        # 一直 MemorySyncBlocked。这里改用带父级的 identity_key 选中，
        # 回复的 source_id 也带上父级，根评论沿用原来的 comment:<id>。
        by_identity = {row.identity_key: row for row in comments}
        root_by_id = {
            row.comment_id: row for row in comments if not row.parent_comment_id
        }
        selected = {
            row.identity_key for row in comments if row.author_uin == OWNER_ID
        }
        for identity in tuple(selected):
            row = by_identity[identity]
            parent_id = row.parent_comment_id
            seen = {identity}
            while parent_id:
                parent = root_by_id.get(parent_id)
                if parent is None:
                    raise MemorySyncBlocked("parent_missing")
                if parent.identity_key in seen:
                    raise MemorySyncBlocked("parent_cycle")
                seen.add(parent.identity_key)
                if parent.author_uin in {OWNER_ID, ASSISTANT_ID}:
                    selected.add(parent.identity_key)
                parent_id = parent.parent_comment_id
        changed = True
        while changed:
            changed = False
            selected_ids = {by_identity[key].comment_id for key in selected}
            for row in comments:
                if (
                    row.author_uin == ASSISTANT_ID
                    and row.parent_comment_id in selected_ids
                    and row.identity_key not in selected
                ):
                    selected.add(row.identity_key)
                    changed = True
        nodes = [
            {
                "source_id": (
                    f"reply:{row.parent_comment_id}:{row.comment_id}"
                    if row.parent_comment_id
                    else f"comment:{row.comment_id}"
                ),
                "remote_id": row.comment_id,
                "parent_id": f"comment:{row.parent_comment_id}"
                if row.parent_comment_id
                else "",
                "author_kind": "owner" if row.author_uin == OWNER_ID else "assistant",
                "author_id": row.author_uin,
                "content": row.content,
                "created_at": "",
                "publication_state": "published",
                "publication_evidence": "counted_remote_thread",
            }
            for row in comments
            if row.identity_key in selected
            and row.author_uin in {OWNER_ID, ASSISTANT_ID}
        ]
        post_id = snapshot["post_id"]
        with self.lock:
            value = self._load()
            previous = value["posts"].get(post_id)
            if previous and revision <= previous["payload"]["revision"]:
                return False
            old = previous["payload"] if previous else {}
            aliases = {}
            for prior in old.get("comments", []):
                if not prior["source_id"].startswith("reply:"):
                    continue
                matches = [
                    node
                    for node in nodes
                    if node["author_id"] == prior["author_id"]
                    and node["parent_id"] == prior["parent_id"]
                    and (node["content"] == prior["content"]
                         or matches_reply_content(node["content"], OWNER_ID, prior["content"]))
                ]
                if len(matches) > 1:
                    raise MemorySyncBlocked("sent_reply_remote_match_ambiguous")
                if matches:
                    aliases[matches[0]["source_id"]] = prior["source_id"]
            prior_by_id = {node["source_id"]: node for node in old.get("comments", [])}
            for node in nodes:
                original_id = node["source_id"]
                node["source_id"] = aliases.get(original_id, original_id)
                if original_id in aliases:
                    node["content"] = prior_by_id[node["source_id"]]["content"]
                node["parent_id"] = aliases.get(node["parent_id"], node["parent_id"])
            retired = set(old.get("deleted_source_ids", [])) | (
                {node["source_id"] for node in old.get("comments", [])}
                - {node["source_id"] for node in nodes}
            )
            payload = {
                "source": "QQ空间",
                "account_id": ASSISTANT_ID,
                "owner_id": OWNER_ID,
                "post_id": post_id,
                "memory_id": _bucket_id("post", post_id),
                "revision": revision,
                "evidence": "verified_remote_thread",
                "deleted": False,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "post": {
                    "source_id": f"post:{post_id}",
                    "remote_id": post_id,
                    "author_kind": "assistant",
                    "author_id": ASSISTANT_ID,
                    "content": snapshot["post_content"],
                    "created_at": "",
                    "parent_id": "",
                    "publication_state": "published",
                    "publication_evidence": "counted_remote_thread",
                },
                "comments": nodes,
                "deleted_source_ids": sorted(retired),
            }
            value["posts"][post_id] = {"payload": payload, "state": "pending"}
            self._save(value)
            return True

    def record_sent(
        self,
        target: Any,
        content: str,
        result: dict | None,
        *,
        verified_by_readback: bool = False,
    ) -> None:
        """Append a confirmed send to its verified thread.

        Args:
            target: Exact confirmed owner reply target.
            content: Actual submitted reply.
            result: Sender's real business status codes, if any.
            verified_by_readback: The exact reply was read back from the
                complete remote thread after an ambiguous business response.

        Raises:
            MemorySyncBlocked: If success or owner scope cannot be verified.
        """
        codes = (
            [result[key] for key in ("code", "ret") if key in result]
            if isinstance(result, dict)
            else []
        )
        business_success = bool(codes) and all(
            type(code) is int and code == 0 for code in codes
        )
        if not verified_by_readback and not business_success:
            raise MemorySyncBlocked("reply_not_confirmed_sent")
        if target.account_uin != ASSISTANT_ID or target.target_uin != OWNER_ID:
            raise MemorySyncBlocked("reply_owner_scope_mismatch")
        with self.lock:
            value = self._load()
            receipt = value["posts"].get(target.post_id)
            root = target.thread_root_comment_id or target.parent_comment_id
            if not receipt or f"comment:{root}" not in {
                node["source_id"] for node in receipt["payload"]["comments"]
            }:
                raise MemorySyncBlocked("verified_reply_parent_missing")
            payload = receipt["payload"]
            existing = [node for node in payload["comments"]
                        if node["author_id"] == ASSISTANT_ID
                        and node["parent_id"] == f"comment:{root}"
                        and matches_reply_content(node["content"], OWNER_ID, content)]
            if len(existing) > 1:
                raise MemorySyncBlocked("sent_reply_remote_match_ambiguous")
            if existing:
                return
            sid = f"reply:{target.idempotency_key}"
            if sid in {node["source_id"] for node in payload["comments"]}:
                return
            value["sequence"] += 1
            payload["revision"] = value["sequence"]
            node = {
                "source_id": sid,
                "remote_id": "",
                "parent_id": f"comment:{root}",
                "author_kind": "assistant",
                "author_id": ASSISTANT_ID,
                "content": content,
                "created_at": "",
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "publication_state": "published",
                "publication_evidence": (
                    "qzone_business_success"
                    if business_success
                    else "verified_remote_thread"
                ),
            }
            if business_success:
                node["business_codes"] = codes
            payload["comments"].append(node)
            receipt["state"] = "pending"
            self._save(value)

    async def flush(self, context: Any, post_id: str) -> str | None:
        """Flush a freshly verified or freshly sent observation locally.

        Args:
            context: Existing AstrBot registry.
            post_id: Exact already authorized post.

        Returns:
            Stable memory ID, or None while Star Map is unavailable.

        Raises:
            MemorySyncBlocked: If a review state must be retained.
        """
        with self.lock:
            receipt = self._load()["posts"].get(post_id)
            if not receipt or receipt["state"] != "pending":
                return None
            payload = json.loads(json.dumps(receipt["payload"]))
        state = "pending"
        try:
            getter = getattr(context, "get_registered_star", None)
            metadata = getter("astrbot_plugin_ob_memory") if callable(getter) else None
            plugin = getattr(metadata, "star_cls", None)
            if plugin is None or getattr(metadata, "activated", True) is False:
                return None
            if str(
                getattr(plugin, "config", {}).get("scope_mode", "conversation")
            ).lower() not in {"hybrid", "user"}:
                raise MemorySyncBlocked("private_user_scope_required")
            service = getattr(plugin, "social_sync", None)
            if not callable(getattr(service, "apply_snapshot", None)):
                return None
            try:
                memory_id = await service.apply_snapshot(payload)
            except ValueError as exc:
                raise MemorySyncBlocked(str(exc)) from exc
            if memory_id:
                state = "complete"
            return memory_id
        except MemorySyncBlocked:
            state = "blocked_policy"
            raise
        finally:
            with self.lock:
                value = self._load()
                current = value["posts"].get(post_id)
                if current and current["payload"]["revision"] == payload["revision"]:
                    current["state"] = state
                    self._save(value)
