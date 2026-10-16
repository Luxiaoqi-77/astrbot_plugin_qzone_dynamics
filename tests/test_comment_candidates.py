"""Offline tests for the isolated Qzone comment candidates."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from data.plugins.astrbot_plugin_qzone_dynamics.comment_candidates import (
    CandidateReadError,
    FutureTopLevelReplyWorkflow,
    FutureTopLevelStateStore,
    inspect_embedded_replies,
    normalize_future_top_level_snapshot,
    parse_subreply_page,
    read_subreply_pages,
)
from data.plugins.astrbot_plugin_qzone_dynamics.comment_workflow import (
    _community_success,
)

ROOT = Path(__file__).parent
BOT_UIN = "2258780898"
OWNER_UIN = "2870791457"
POST_ID = "post-1"


def _future_snapshot(
    comment_id: str = "root-2", content: str = "新的顶层评论"
) -> dict[str, object]:
    return {
        "account_uin": BOT_UIN,
        "post_id": POST_ID,
        "post_author_uin": BOT_UIN,
        "post_content": "公开动态正文",
        "comment": {
            "tid": comment_id,
            "uin": OWNER_UIN,
            "content": content,
        },
    }


class SubreplyCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = json.loads(
            (ROOT / "fixtures" / "subreply_pages.json").read_text()
        )

    def test_explicit_has_more_false_is_reliable_termination(self) -> None:
        async def fetch(cursor: str | None) -> dict[str, object]:
            return self.fixtures["explicit_two_pages"][0 if cursor is None else 1]

        result = asyncio.run(
            read_subreply_pages(fetch, parent_comment_id="root-1", page_size=1)
        )
        self.assertEqual(result["termination"], "explicit_has_more_false")
        self.assertEqual(len(result["replies"]), 2)
        self.assertEqual(result["reply_count_hints"], (2,))

    def test_short_page_without_marker_remains_unknown(self) -> None:
        async def fetch(cursor: str | None) -> dict[str, object]:
            return self.fixtures["short_without_terminator"][0]

        with self.assertRaisesRegex(
            CandidateReadError, "reply_pagination_termination_unknown"
        ):
            asyncio.run(
                read_subreply_pages(fetch, parent_comment_id="root-1", page_size=20)
            )

    def test_embedded_list_three_is_observation_not_completion(self) -> None:
        result = inspect_embedded_replies(self.fixtures["embedded_root"])
        self.assertFalse(result["count_hint_matches"])
        self.assertEqual(result["completeness"], "unknown")

    def test_incomplete_community_success_response_is_unknown(self) -> None:
        self.assertFalse(_community_success({"code": 0}))
        self.assertFalse(_community_success({"ret": 0}))
        self.assertTrue(_community_success({"code": 0, "ret": 0}))


class FutureRootCandidateTests(unittest.TestCase):
    def test_new_root_does_not_need_historical_subreplies(self) -> None:
        async def loader(
            post_id: str, comment_id: str, author_uin: str
        ) -> dict[str, object]:
            return _future_snapshot(comment_id)

        async def generator(comment) -> str:
            return f"收到：{comment.comment_content}"

        async def sender(target, content):
            raise AssertionError("dry-run candidate must not call sender")

        with tempfile.TemporaryDirectory() as temporary:
            state = FutureTopLevelStateStore(Path(temporary) / "future-root.json")
            generation = state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            workflow = FutureTopLevelReplyWorkflow(
                enabled=True,
                dry_run=True,
                state=state,
                snapshot_loader=loader,
                draft_generator=generator,
                sender=sender,
            )
            draft = asyncio.run(
                workflow.prepare_reply(
                    _future_snapshot(),
                    account_uin=BOT_UIN,
                    post_id=POST_ID,
                    comment_id="root-2",
                    owner_actor_uin=OWNER_UIN,
                )
            )
            self.assertEqual(draft.baseline_generation, generation)
            self.assertEqual(draft.status, "pending")
            with self.assertRaisesRegex(RuntimeError, "dry_run"):
                asyncio.run(
                    workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
                )

    def test_baseline_root_is_not_treated_as_new(self) -> None:
        async def generator(comment) -> str:
            return "候选"

        with tempfile.TemporaryDirectory() as temporary:
            state = FutureTopLevelStateStore(Path(temporary) / "future-root.json")
            state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            workflow = FutureTopLevelReplyWorkflow(
                enabled=True,
                state=state,
                snapshot_loader=None,
                draft_generator=generator,
                sender=None,
            )
            with self.assertRaisesRegex(CandidateReadError, "before_enable_baseline"):
                asyncio.run(
                    workflow.prepare_reply(
                        _future_snapshot("root-1"),
                        account_uin=BOT_UIN,
                        post_id=POST_ID,
                        comment_id="root-1",
                        owner_actor_uin=OWNER_UIN,
                    )
                )

    def test_send_reread_change_invalidates_before_sender(self) -> None:
        async def loader(
            post_id: str, comment_id: str, author_uin: str
        ) -> dict[str, object]:
            return _future_snapshot(comment_id, "内容已经变化")

        async def generator(comment) -> str:
            return "候选"

        async def sender(target, content):
            raise AssertionError("changed root must not send")

        with tempfile.TemporaryDirectory() as temporary:
            state = FutureTopLevelStateStore(Path(temporary) / "future-root.json")
            state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            workflow = FutureTopLevelReplyWorkflow(
                enabled=True,
                dry_run=False,
                state=state,
                snapshot_loader=loader,
                draft_generator=generator,
                sender=sender,
            )
            draft = asyncio.run(
                workflow.prepare_reply(
                    _future_snapshot(),
                    account_uin=BOT_UIN,
                    post_id=POST_ID,
                    comment_id="root-2",
                    owner_actor_uin=OWNER_UIN,
                )
            )
            with self.assertRaisesRegex(CandidateReadError, "root_changed"):
                asyncio.run(
                    workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
                )
            self.assertEqual(state.get_draft(draft.draft_id).status, "invalidated")

    def test_success_is_one_shot_and_persisted(self) -> None:
        calls: list[str] = []

        async def loader(
            post_id: str, comment_id: str, author_uin: str
        ) -> dict[str, object]:
            return _future_snapshot(comment_id)

        async def generator(comment) -> str:
            return "候选"

        async def sender(target, content):
            calls.append(target.idempotency_key)
            return {"code": 0, "ret": 0}

        with tempfile.TemporaryDirectory() as temporary:
            state = FutureTopLevelStateStore(Path(temporary) / "future-root.json")
            state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            workflow = FutureTopLevelReplyWorkflow(
                enabled=True,
                dry_run=False,
                state=state,
                snapshot_loader=loader,
                draft_generator=generator,
                sender=sender,
            )
            draft = asyncio.run(
                workflow.prepare_reply(
                    _future_snapshot(),
                    account_uin=BOT_UIN,
                    post_id=POST_ID,
                    comment_id="root-2",
                    owner_actor_uin=OWNER_UIN,
                )
            )
            result = asyncio.run(
                workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
            )
            self.assertEqual(result.status, "sent")
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                asyncio.run(
                    workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
                )

    def test_incomplete_success_is_delivery_unknown_and_not_retried(self) -> None:
        calls: list[str] = []

        async def loader(
            post_id: str, comment_id: str, author_uin: str
        ) -> dict[str, object]:
            return _future_snapshot(comment_id)

        async def generator(comment) -> str:
            return "候选"

        async def sender(target, content):
            calls.append(target.idempotency_key)
            return {"code": 0}

        with tempfile.TemporaryDirectory() as temporary:
            state = FutureTopLevelStateStore(Path(temporary) / "future-root.json")
            state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            workflow = FutureTopLevelReplyWorkflow(
                enabled=True,
                dry_run=False,
                state=state,
                snapshot_loader=loader,
                draft_generator=generator,
                sender=sender,
            )
            draft = asyncio.run(
                workflow.prepare_reply(
                    _future_snapshot(),
                    account_uin=BOT_UIN,
                    post_id=POST_ID,
                    comment_id="root-2",
                    owner_actor_uin=OWNER_UIN,
                )
            )
            result = asyncio.run(
                workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
            )
            self.assertEqual(result.status, "delivery_unknown")
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                asyncio.run(
                    workflow.send_confirmed(draft.draft_id, draft.confirmation_token)
                )

    def test_identity_and_parent_alias_conflicts_fail_closed(self) -> None:
        row = {"tid": "11", "uin": OWNER_UIN, "content": "reply"}
        with self.assertRaisesRegex(CandidateReadError, "reply_id_ambiguous"):
            parse_subreply_page(
                {"comments": [{**row, "commentid": "12"}]},
                parent_comment_id="root-1",
            )
        with self.assertRaisesRegex(CandidateReadError, "reply_parent_missing"):
            parse_subreply_page(
                {"comments": [{**row, "replyid": 0}]},
                parent_comment_id="root-1",
            )

        snapshot = _future_snapshot()
        snapshot["comment"] = {**snapshot["comment"], "commentid": "other"}
        with self.assertRaisesRegex(CandidateReadError, "comment_id_ambiguous"):
            normalize_future_top_level_snapshot(
                snapshot,
                account_uin=BOT_UIN,
                expected_post_id=POST_ID,
                expected_comment_id="root-2",
                expected_author_uin=OWNER_UIN,
            )

        snapshot = _future_snapshot()
        snapshot["comment"] = {**snapshot["comment"], "replyid": "root-1"}
        with self.assertRaisesRegex(CandidateReadError, "future_root_not_top_level"):
            normalize_future_top_level_snapshot(
                snapshot,
                account_uin=BOT_UIN,
                expected_post_id=POST_ID,
                expected_comment_id="root-2",
                expected_author_uin=OWNER_UIN,
            )

    def test_baseline_binding_and_legacy_state_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "future-root.json"
            state = FutureTopLevelStateStore(path)
            state.enable_baseline(
                {POST_ID: ["root-1"]},
                account_uin=BOT_UIN,
                post_author_uin=BOT_UIN,
                complete=True,
            )
            with self.assertRaisesRegex(RuntimeError, "account binding"):
                state.baseline_for(
                    POST_ID,
                    account_uin="2258780899",
                    post_author_uin=BOT_UIN,
                )

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "generation": 1,
                        "baseline": {POST_ID: ["root-1"]},
                        "drafts": {},
                        "receipts": {},
                    }
                )
            )
            legacy_bytes = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "account-bound migration"):
                state.baseline_for(
                    POST_ID,
                    account_uin=BOT_UIN,
                    post_author_uin=BOT_UIN,
                )
            self.assertEqual(path.read_bytes(), legacy_bytes)


if __name__ == "__main__":
    unittest.main()
