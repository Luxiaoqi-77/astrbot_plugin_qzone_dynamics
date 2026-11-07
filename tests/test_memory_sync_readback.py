"""Offline tests for read-back-verified Qzone replies in the local outbox."""

from __future__ import annotations

import json

import pytest
from astrbot_plugin_qzone_dynamics.comment_reply import ExactReplyTarget
from astrbot_plugin_qzone_dynamics.comment_workflow import normalize_comment_thread
from astrbot_plugin_qzone_dynamics.memory_sync import (
    ASSISTANT_ID,
    OWNER_ID,
    MemorySyncBlocked,
    QzoneMemoryOutbox,
)

POST = "post1"


def _payload() -> dict:
    return {
        "source": "QQ空间",
        "account_id": ASSISTANT_ID,
        "owner_id": OWNER_ID,
        "post_id": POST,
        "memory_id": "qz_test",
        "revision": 5,
        "evidence": "verified_remote_thread",
        "deleted": False,
        "post": {
            "source_id": f"post:{POST}",
            "author_kind": "assistant",
            "author_id": ASSISTANT_ID,
            "content": "合成动态正文",
            "parent_id": "",
            "publication_state": "published",
        },
        "comments": [
            {
                "source_id": "comment:1",
                "author_kind": "owner",
                "author_id": OWNER_ID,
                "content": "合成评论",
                "parent_id": "",
                "publication_state": "published",
            }
        ],
        "deleted_source_ids": [],
    }


def _outbox(tmp_path):
    path = tmp_path / "outbox.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sequence": 5,
                "posts": {POST: {"payload": _payload(), "state": "pending"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return QzoneMemoryOutbox(path), path


def _target() -> ExactReplyTarget:
    return ExactReplyTarget(
        account_uin=ASSISTANT_ID,
        post_id=POST,
        parent_comment_id="1",
        target_uin=OWNER_ID,
        thread_fingerprint="fp",
        idempotency_key="idem",
    )


def test_readback_verified_reply_is_recorded_without_business_codes(tmp_path):
    outbox, path = _outbox(tmp_path)

    outbox.record_sent(_target(), "合成回复", None, verified_by_readback=True)

    stored = json.loads(path.read_text(encoding="utf-8"))
    node = stored["posts"][POST]["payload"]["comments"][-1]
    assert node["content"] == "合成回复"
    assert node["publication_evidence"] == "verified_remote_thread"
    assert "business_codes" not in node


def test_duplicate_root_and_reply_ids_do_not_block_observation(tmp_path):
    outbox, path = _outbox(tmp_path)
    snapshot = normalize_comment_thread(
        {
            "account_uin": ASSISTANT_ID,
            "post_id": POST,
            "post_author_uin": ASSISTANT_ID,
            "post_content": "合成动态正文",
            "declared_comment_count": 2,
            "server_count_evidence": {"detail.totalCommentCount": 2},
            "comments": [
                {
                    "comment_id": "1",
                    "author_uin": OWNER_ID,
                    "content": "合成评论",
                    "parent_comment_id": "",
                },
                {
                    "comment_id": "1",
                    "author_uin": ASSISTANT_ID,
                    "content": "合成回复",
                    "parent_comment_id": "1",
                },
            ],
        }
    )

    assert outbox.observe(snapshot, 6) is True

    stored = json.loads(path.read_text(encoding="utf-8"))
    comments = stored["posts"][POST]["payload"]["comments"]
    assert [node["source_id"] for node in comments] == ["comment:1", "reply:1:1"]
    assert [node["parent_id"] for node in comments] == ["", "comment:1"]


def test_unconfirmed_business_failure_still_raises(tmp_path):
    outbox, _ = _outbox(tmp_path)

    with pytest.raises(MemorySyncBlocked):
        outbox.record_sent(_target(), "合成回复", {"code": -1})
