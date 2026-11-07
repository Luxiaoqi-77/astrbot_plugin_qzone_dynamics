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
                "cmtnum": total,
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


class RetryTransport(SyntheticTransport):
    """Return an API error first, then success after the page token is read."""

    def __init__(self, feed, home_html: str, first_detail, second_detail):
        super().__init__(feed, first_detail)
        self.home_html = home_html
        self.second_detail = json.dumps(second_detail).encode()
        self.detail_calls = 0

    async def request(self, endpoint_key, *, query, **kwargs):
        if endpoint_key == "get_qzone_home":
            self.calls.append((endpoint_key, dict(query)))
            return self.home_html.encode()
        if endpoint_key == "get_post_detail":
            self.calls.append((endpoint_key, dict(query)))
            self.detail_calls += 1
            if self.detail_calls >= 2:
                return self.second_detail
            return self.responses[endpoint_key]
        return await super().request(endpoint_key, query=query, **kwargs)


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

    def test_feed_miss_falls_back_to_detail_by_post_id(self) -> None:
        transport = SyntheticTransport(
            {"code": 0, "msglist": []},
            {
                "ret": 0,
                "uin": BOT_UIN,
                "tid": POST_ID,
                "content": "合成动态正文",
                "cmtnum": 1,
                "commentlist": [
                    {
                        "tid": "1",
                        "uin": OWNER_UIN,
                        "content": "合成评论",
                        "replyNum": 0,
                    }
                ],
            },
        )

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["post_content"], "合成动态正文")
        self.assertEqual(result["declared_comment_count"], 1)
        self.assertEqual(
            [row["comment_identity_key"] for row in result["comments"]],
            ["top_level:1"],
        )
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["list_owner_feeds", "get_post_detail"],
        )

    def test_feed_api_error_falls_back_to_detail(self) -> None:
        transport = SyntheticTransport(
            {"code": -1},
            {
                "ret": 0,
                "uin": BOT_UIN,
                "tid": POST_ID,
                "content": "合成动态正文",
                "cmtnum": 1,
                "commentlist": [
                    {
                        "tid": "1",
                        "uin": OWNER_UIN,
                        "content": "合成评论",
                        "replyNum": 0,
                    }
                ],
            },
        )

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["post_content"], "合成动态正文")
        self.assertEqual(result["declared_comment_count"], 1)
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["list_owner_feeds", "get_post_detail"],
        )

    def test_zero_comment_feed_avoids_detail_api(self) -> None:
        feed = {
            "code": 0,
            "msglist": [
                {
                    "tid": POST_ID,
                    "uin": BOT_UIN,
                    "content": "合成动态正文",
                    "cmtnum": 0,
                }
            ],
        }
        transport = SyntheticTransport(feed, {"code": -10029})

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["declared_comment_count"], 0)
        self.assertEqual(result["comments"], [])
        self.assertEqual(
            [call[0] for call in transport.calls], ["list_owner_feeds"]
        )

    def test_feed_commentlist_without_replynum_is_accepted(self) -> None:
        feed = {
            "code": 0,
            "msglist": [
                {
                    "tid": POST_ID,
                    "uin": BOT_UIN,
                    "content": "合成动态正文",
                    "cmtnum": 1,
                    "has_more_con": False,
                    "commentlist": [
                        {
                            "tid": "1",
                            "uin": OWNER_UIN,
                            "content": "合成评论",
                            "list_3": [
                                {
                                    "tid": "1",
                                    "uin": BOT_UIN,
                                    "content": "合成回复",
                                    "replyid": "1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        transport = SyntheticTransport(feed, {"code": -10029})

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["declared_comment_count"], 2)
        self.assertEqual(
            [row["comment_identity_key"] for row in result["comments"]],
            ["top_level:1", "reply:1:1"],
        )

    def test_detail_api_error_retries_with_page_token(self) -> None:
        transport = RetryTransport(
            {"code": 0, "msglist": []},
            'window.g_qzonetoken = (function(){ try{return "TOKEN123";}'
            'catch(e){return "";} })();',
            {"code": -10029},
            {
                "ret": 0,
                "uin": BOT_UIN,
                "tid": POST_ID,
                "content": "合成动态正文",
                "cmtnum": 1,
                "commentlist": [
                    {
                        "tid": "1",
                        "uin": OWNER_UIN,
                        "content": "合成评论",
                        "replyNum": 0,
                    }
                ],
            },
        )

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["post_content"], "合成动态正文")
        detail_queries = [
            query for key, query in transport.calls if key == "get_post_detail"
        ]
        self.assertEqual(len(detail_queries), 2)
        self.assertNotIn("qzonetoken", detail_queries[0])
        self.assertEqual(detail_queries[1]["qzonetoken"], "TOKEN123")
        self.assertEqual(detail_queries[1]["callback"], "_preloadCallback")

    def test_detail_api_error_uses_feed_commentlist(self) -> None:
        feed = {
            "code": 0,
            "msglist": [
                {
                    "tid": POST_ID,
                    "uin": BOT_UIN,
                    "content": "合成动态正文",
                    "cmtnum": 1,
                    "commentlist": [
                        {
                            "tid": "1",
                            "uin": OWNER_UIN,
                            "content": "合成评论",
                            "replyNum": 1,
                            "list_3": [
                                {
                                    "tid": "1",
                                    "uin": BOT_UIN,
                                    "content": "合成回复",
                                    "replyid": "1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        transport = SyntheticTransport(feed, {"code": -10029})

        result = asyncio.run(
            QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
        )

        self.assertEqual(result["post_content"], "合成动态正文")
        self.assertEqual(result["declared_comment_count"], 2)
        self.assertEqual(
            [row["comment_identity_key"] for row in result["comments"]],
            ["top_level:1", "reply:1:1"],
        )

    def test_detail_api_error_has_distinct_reason(self) -> None:
        transport = SyntheticTransport(_feed(1), {"code": -1})

        with self.assertRaisesRegex(
            QzoneCommentReadError, "comment_detail_api_error"
        ):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
            )

    def test_feed_ambiguity_still_fails_before_detail(self) -> None:
        feed = {
            "code": 0,
            "msglist": [
                {"tid": POST_ID, "uin": BOT_UIN, "content": "一"},
                {"tid": POST_ID, "uin": BOT_UIN, "content": "二"},
            ],
        }
        transport = SyntheticTransport(feed, _detail(0, []))

        with self.assertRaisesRegex(
            QzoneCommentReadError, "comment_post_not_found_or_ambiguous"
        ):
            asyncio.run(
                QzoneCommentReader(transport).load_full_thread(BOT_UIN, POST_ID)
            )
        self.assertEqual([call[0] for call in transport.calls], ["list_owner_feeds"])

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
