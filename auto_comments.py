"""Owner-only automatic replies with durable baseline and one-attempt receipts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .comment_reply import bind_reply_target, clean_comment_text, comment_identity_key
from .comment_workflow import _community_success, normalize_comment_thread


def _thread_context(rows, *, account_uin: str, owner_uin: str) -> str:
    """Render the verified thread as labelled data for the reply prompt.

    2026-10-20：楼内追问也要带上下文。只给作者标签和正文，不输出任何账号 ID。
    """
    by_id = {row.comment_id: row for row in rows}
    lines: list[str] = []
    for row in sorted(rows, key=lambda item: (item.parent_comment_id, item.comment_id)):
        depth = 0
        parent = row.parent_comment_id
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            depth += 1
            parent_row = by_id.get(parent)
            parent = parent_row.parent_comment_id if parent_row else ""
        if row.author_uin == owner_uin:
            speaker = "主人"
        elif row.author_uin == account_uin:
            speaker = "你（小回）"
        else:
            speaker = "其他人"
        lines.append(f"{'  ' * depth}{speaker}: {clean_comment_text(row.content)[:200]}")
    return "\n".join(lines)[:6000]


class AutoCommentReplies:
    """Observe new root comments and reuse the exact-target send boundary."""

    def __init__(self, path: Path, *, account_uin: str, owner_uin: str):
        self.path = path
        self.account_uin = account_uin
        self.owner_uin = owner_uin
        self.lock = asyncio.Lock()

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "posts": {}}
        state = json.loads(self.path.read_text())
        if state.get("schema_version") != 1 or not isinstance(state.get("posts"), dict):
            raise ValueError("automatic comment state is invalid")
        return state

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    async def _verify_delivered(self, workflow, post_id: str, target, content: str) -> str:
        """Confirm a reply by reading it back; failure keeps the safe unknown state.

        QQ 空间评论列表有缓存/延迟，刚发完立刻读可能还没出现。这里最多回读三次，
        避免把“已经发出去但还没刷新”误记成 delivery_unknown。
        """
        root = target.thread_root_comment_id or target.parent_comment_id
        expected = clean_comment_text(content)
        for wait_seconds in (1.0, 2.0, 4.0):
            await asyncio.sleep(wait_seconds)
            try:
                snapshot = normalize_comment_thread(
                    await workflow.thread_loader(self.account_uin, post_id)
                )
            except Exception:
                continue
            if any(
                comment.author_uin == self.account_uin
                and comment.parent_comment_id == root
                and clean_comment_text(comment.content) == expected
                for comment in snapshot["comments"]
            ):
                return "sent"
        return "delivery_unknown"

    async def poll_post(self, workflow, post_id: str, *, ready=lambda: True):
        """Read a complete thread and attempt at most one new owner root per poll.

        Args:
            workflow: Existing reader, generator, sender and durable receipts.
            post_id: A post already published by this plugin.
            ready: Recheck the runtime opt-in before generation and sending.

        Returns:
            A status code without any comment or credential text.
        """
        async with self.lock:
            if not ready():
                return "disabled"
            snapshot = normalize_comment_thread(
                await workflow.thread_loader(self.account_uin, post_id)
            )
            if (
                snapshot["account_uin"] != self.account_uin
                or snapshot["post_author_uin"] != self.account_uin
                or snapshot["post_id"] != post_id
            ):
                raise ValueError("automatic comment account or post mismatch")
            state = self._load()
            rows = snapshot["comments"]
            # 2026-10-20：主评论和楼内追问都算目标。主人回复小回的回复时，
            # 那条追问同样会触发一次新的自动回复。
            if post_id not in state["posts"]:
                state["posts"][post_id] = {
                    comment_identity_key(c.comment_id, c.parent_comment_id): "baseline"
                    for c in rows
                }
                self._save(state)
                return "baseline"
            seen = state["posts"][post_id]
            for comment in rows:
                key = comment_identity_key(
                    comment.comment_id, comment.parent_comment_id
                )
                if key in seen:
                    continue
                if comment.author_uin != self.owner_uin:
                    seen[key] = "other_author"
                    self._save(state)
                    continue
                if any(
                    c.author_uin == self.account_uin
                    and c.parent_comment_id == comment.comment_id
                    for c in rows
                ):
                    seen[key] = "already_replied"
                    self._save(state)
                    continue
                target = bind_reply_target(
                    account_uin=self.account_uin,
                    post_id=post_id,
                    post_author_uin=self.account_uin,
                    post_content=snapshot["post_content"],
                    comment_id=comment.comment_id,
                    expected_target_uin=self.owner_uin,
                    declared_comment_count=snapshot["declared_comment_count"],
                    comments=rows,
                    comment_parent_id=comment.parent_comment_id,
                )
                if workflow.receipts.contains(target):
                    seen[key] = "claimed"
                    self._save(state)
                    continue
                if not ready():
                    return "disabled"
                # Persist before a paid generation: a crash cannot repeatedly bill
                # the same comment. Only metadata, never the comment text, is saved.
                seen[key] = "generating"
                self._save(state)
                try:
                    content = await workflow.draft_generator(
                        comment,
                        snapshot["post_content"],
                        thread_context=_thread_context(
                            rows,
                            account_uin=self.account_uin,
                            owner_uin=self.owner_uin,
                        ),
                    )
                    if (
                        not isinstance(content, str)
                        or not 0 < len(content.strip()) <= 20
                    ):
                        raise ValueError(
                            "automatic reply must contain 1 to 20 characters"
                        )
                    current = normalize_comment_thread(
                        await workflow.thread_loader(self.account_uin, post_id)
                    )
                    refreshed = bind_reply_target(
                        account_uin=current["account_uin"],
                        post_id=current["post_id"],
                        post_author_uin=current["post_author_uin"],
                        post_content=current["post_content"],
                        comment_id=comment.comment_id,
                        expected_target_uin=self.owner_uin,
                        declared_comment_count=current["declared_comment_count"],
                        comments=current["comments"],
                        comment_parent_id=comment.parent_comment_id,
                    )
                    if refreshed != target:
                        raise ValueError("automatic reply thread changed before send")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    seen[key] = "generation_or_recheck_failed"
                    self._save(state)
                    return seen[key]
                if not ready():
                    seen[key] = "disabled_before_send"
                    self._save(state)
                    return seen[key]
                if not workflow.receipts.claim(target):
                    seen[key] = "claimed"
                    self._save(state)
                    return seen[key]
                status = "delivery_unknown"
                try:
                    result = await workflow.sender(target, content.strip())
                    if _community_success(result):
                        status = "sent"
                    else:
                        # QQ 的回复接口返回体并不稳定；发送后间隔回读线程，
                        # 以“真的出现了这条回复”为准，避免误判 unknown 后不再重试。
                        status = await self._verify_delivered(
                            workflow, post_id, target, content.strip()
                        )
                except asyncio.CancelledError:
                    workflow.receipts.finish(target, status=status)
                    seen[key] = status
                    self._save(state)
                    raise
                except Exception:
                    pass
                workflow.receipts.finish(target, status=status)
                seen[key] = status
                self._save(state)
                if status == "sent" and workflow.on_sent is not None:
                    await workflow.on_sent(target, content.strip(), result)
                return status
            self._save(state)
            return "idle"
