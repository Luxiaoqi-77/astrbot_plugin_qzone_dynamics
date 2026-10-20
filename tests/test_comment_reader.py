"""Offline tests for the synthetic Qzone comment reader fixtures."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from data.plugins.astrbot_plugin_qzone_dynamics.comment_reader import (
    QzoneCommentReader,
    QzoneCommentReadError,
    flatten_detail_comments,
)

BOT_UIN = "100000002"
OWNER_UIN = "100000001"
POST_ID = "post-1"


class SyntheticTransport:
    """In-memory transport for synthetic responses; it never contacts Qzone."""

    enabled = True

    def __init__(self, feed: dict[str, object], detail: dict[str, object]):
        self.credential_store = SimpleNamespace(account_uin=BOT_UIN)
        self.responses = {
            "list_owner_feeds": json.dumps(feed).encode(),
            "get_post_detail": json.dumps(detail).encode(),
        }
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def request(
        self, endpoint_key: str, *, query: dict[str, str], **kwargs
    ) -> bytes:
        self.calls.append((endpoint_key, dict(query)))
        return self.responses[endpoint_key]


def _feed(total: int) -> dict[str, object]:
    return {
        "code": 0,
        "msglist": [
            {
                "tid": POST_ID,
                "uin": BOT_UIN,
                "content": "合成动态正文",
                "total_comment_count": total,
            }
        ],
    }


def _detail(total: int, comments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ret": 0,
        "uin": BOT_UIN,
        "tid": POST_ID,
        "totalCommentCount": total,
        "commentlist": comments,
    }


class QzoneCommentReaderTests(unittest.TestCase):
    def test_synthetic_complete_thread_uses_exact_owner_and_post(self) -> None:
        transport = SyntheticTransport(
            _feed(2),
            _detail(
                2,
                [
                    {
                        "tid": "101",
                        "uin": OWNER_UIN,
                        "content": "合成顶层评论",
                        "list_3": [
                            {
                                "tid": "201",
                                "uin": OWNER_UIN,
                                "content": "合成子回复",
                                "replyid": "101",
                            }
                        ],
                    }
                ],
            ),
        )

        result = asyncio.run(
            QzoneCommentReader(transport, max_comments=2).load_full_thread(
                BOT_UIN, POST_ID
            )
        )

        self.assertEqual(result["declared_comment_count"], 2)
        self.assertEqual(
            [row["comment_identity_key"] for row in result["comments"]],
            ["top_level:101", "reply:101:201"],
        )
        self.assertEqual(transport.calls[0][1]["pos"], "0")
        self.assertEqual(transport.calls[0][1]["num"], "100")

    def test_synthetic_count_truncation_is_rejected(self) -> None:
        transport = SyntheticTransport(
            _feed(2),
            _detail(
                2,
                [{"tid": "101", "uin": OWNER_UIN, "content": "只有一条"}],
            ),
        )

        with self.assertRaisesRegex(QzoneCommentReadError, "comment_thread_truncated"):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
            )

    def test_synthetic_explicit_empty_parent_is_not_top_level_evidence(self) -> None:
        with self.assertRaisesRegex(QzoneCommentReadError, "comment_parent_invalid"):
            flatten_detail_comments(
                [
                    {
                        "tid": "101",
                        "uin": OWNER_UIN,
                        "content": "合成评论",
                        "reply_comment_id": "",
                    }
                ]
            )

    def test_synthetic_conflicting_child_parent_aliases_are_rejected(self) -> None:
        with self.assertRaisesRegex(QzoneCommentReadError, "comment_parent_invalid"):
            flatten_detail_comments(
                [
                    {
                        "tid": "101",
                        "uin": OWNER_UIN,
                        "content": "合成评论",
                        "list_3": [
                            {
                                "tid": "201",
                                "uin": OWNER_UIN,
                                "content": "合成回复",
                                "replyid": "101",
                                "reply_comment_id": "102",
                            }
                        ],
                    }
                ]
            )

    def test_synthetic_duplicate_top_level_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(QzoneCommentReadError, "comment_id_ambiguous"):
            flatten_detail_comments(
                [
                    {"tid": "101", "uin": OWNER_UIN, "content": "一"},
                    {"tid": "101", "uin": OWNER_UIN, "content": "二"},
                ]
            )

    def test_synthetic_owner_mismatch_is_rejected_before_transport(self) -> None:
        transport = SyntheticTransport(_feed(0), _detail(0, []))
        with self.assertRaisesRegex(QzoneCommentReadError, "comment_owner_mismatch"):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread("2258780899", POST_ID)
            )
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()


class NativeQzoneCounterTests(unittest.TestCase):
    """Native root and per-root child counts are different from thread totals."""

    def transport(self, *, roots=1, children=0, listed_children=None):
        feed = {
            "code": 0,
            "msglist": [
                {"tid": POST_ID, "uin": BOT_UIN, "content": "合成动态", "cmtnum": roots}
            ],
        }
        detail = {"code": 0, "uin": BOT_UIN, "tid": POST_ID, "cmtnum": roots}
        if roots:
            detail["commentlist"] = [
                {
                    "tid": "1",
                    "uin": OWNER_UIN,
                    "content": "合成评论",
                    "replyNum": children,
                }
            ]
            if children or listed_children is not None:
                count = children if listed_children is None else listed_children
                detail["commentlist"][0]["list_3"] = [
                    {"tid": str(i + 10), "uin": BOT_UIN, "content": "合成回复"}
                    for i in range(count)
                ]
        return SyntheticTransport(feed, detail)

    def test_native_zero_roots_may_omit_commentlist(self):
        result = asyncio.run(
            QzoneCommentReader(self.transport(roots=0)).load_full_thread(
                BOT_UIN, POST_ID
            )
        )
        self.assertEqual(result["declared_comment_count"], 0)
        self.assertEqual(result["comments"], [])

    def test_native_root_without_replies(self):
        result = asyncio.run(
            QzoneCommentReader(self.transport()).load_full_thread(BOT_UIN, POST_ID)
        )
        self.assertEqual(result["declared_comment_count"], 1)

    def test_native_child_count_is_added_to_root_count(self):
        result = asyncio.run(
            QzoneCommentReader(self.transport(children=1)).load_full_thread(
                BOT_UIN, POST_ID
            )
        )
        self.assertEqual(result["declared_comment_count"], 2)
        self.assertEqual(len(result["comments"]), 2)

    def test_native_truncated_children_rejected(self):
        with self.assertRaises(QzoneCommentReadError):
            asyncio.run(
                QzoneCommentReader(
                    self.transport(children=2, listed_children=1)
                ).load_full_thread(BOT_UIN, POST_ID)
            )

    def test_native_missing_child_counter_rejected(self):
        transport = self.transport()
        detail = json.loads(transport.responses["get_post_detail"])
        del detail["commentlist"][0]["replyNum"]
        transport.responses["get_post_detail"] = json.dumps(detail).encode()
        with self.assertRaises(QzoneCommentReadError):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
            )

    def test_native_root_counts_conflict_rejected(self):
        transport = self.transport()
        detail = json.loads(transport.responses["get_post_detail"])
        detail["cmtnum"] = 0
        transport.responses["get_post_detail"] = json.dumps(detail).encode()
        with self.assertRaises(QzoneCommentReadError):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
            )
