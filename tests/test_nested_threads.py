"""Real-shape QQ Space threads: replies reuse the top-level comment ID."""

from __future__ import annotations

from astrbot_plugin_qzone_dynamics.comment_reply import (
    bind_reply_target,
    clean_comment_text,
)
from astrbot_plugin_qzone_dynamics.comment_workflow import normalize_comment_thread

BOT = "100000002"
OWNER = "100000001"


def _raw(rows):
    return {
        "account_uin": BOT,
        "post_id": "post1",
        "post_author_uin": BOT,
        "post_content": "阳光好好～",
        "declared_comment_count": len(rows),
        "server_count_evidence": {"detail.totalCommentCount": len(rows)},
        "comments": rows,
    }


def _row(comment_id, uin, content, parent=""):
    return {
        "commentid": comment_id,
        "uin": uin,
        "content": content,
        "parent_comment_id": parent,
    }


def test_reply_may_reuse_the_top_level_comment_id():
    # 2026-10-20 真实返回：主评论 id=1，小回的楼内回复 id 也是 1。
    snapshot = normalize_comment_thread(
        _raw(
            [
                _row("1", OWNER, "阳光好好～"),
                _row("1", BOT, "嗯，适合分你一半，一起发呆。", "1"),
                _row("2", OWNER, "@{uin:100000002,nick:沈星回,who:1,auto:1}不去", "1"),
            ]
        )
    )
    keys = [comment.identity_key for comment in snapshot["comments"]]
    assert keys == ["top_level:1", "reply:1:1", "reply:1:2"]


def test_nested_owner_reply_can_be_bound_as_target():
    snapshot = normalize_comment_thread(
        _raw(
            [
                _row("1", OWNER, "阳光好好～"),
                _row("1", BOT, "嗯，适合分你一半，一起发呆。", "1"),
                _row("2", OWNER, "@{uin:100000002,nick:沈星回,who:1,auto:1}不去", "1"),
            ]
        )
    )
    target = bind_reply_target(
        account_uin=BOT,
        post_id="post1",
        post_author_uin=BOT,
        post_content=snapshot["post_content"],
        comment_id="2",
        expected_target_uin=OWNER,
        declared_comment_count=snapshot["declared_comment_count"],
        comments=snapshot["comments"],
        comment_parent_id="1",
    )
    assert target.parent_comment_id == "2"
    assert target.parent_identity_key == "reply:1:2"
    # QQ 的回复接口只认主评论 ID（楼层是两层结构）。
    assert target.thread_root_comment_id == "1"


def test_deep_reply_chain_is_allowed():
    snapshot = normalize_comment_thread(
        _raw(
            [
                _row("1", OWNER, "主评论"),
                _row("2", BOT, "小回回复", "1"),
                _row("3", OWNER, "主人追问", "2"),
                _row("4", BOT, "再回一句", "3"),
            ]
        )
    )
    assert len(snapshot["comments"]) == 4


def test_mention_markup_is_readable():
    assert clean_comment_text(
        "@{uin:100000002,nick:沈星回,who:1,auto:1}不去"
    ) == "@沈星回不去"


def test_reply_sender_uses_the_thread_root_comment_id():
    import asyncio

    from astrbot_plugin_qzone_dynamics.comment_reader import QzoneCommentReplySender

    snapshot = normalize_comment_thread(
        _raw(
            [
                _row("1", OWNER, "阳光好好～"),
                _row("1", BOT, "嗯，适合分你一半，一起发呆。", "1"),
                _row("2", OWNER, "@{uin:100000002,nick:沈星回,who:1,auto:1}不去", "1"),
            ]
        )
    )
    target = bind_reply_target(
        account_uin=BOT,
        post_id="post1",
        post_author_uin=BOT,
        post_content=snapshot["post_content"],
        comment_id="2",
        expected_target_uin=OWNER,
        declared_comment_count=snapshot["declared_comment_count"],
        comments=snapshot["comments"],
        comment_parent_id="1",
    )

    class _Transport:
        enabled = True

        class _Cred:
            account_uin = BOT

        credential_store = _Cred()

        def __init__(self):
            self.form = None

        async def request(self, endpoint_key, *, query=None, data=None, extra_headers=None):
            self.form = data
            return b'{"code":0,"ret":0}'

    transport = _Transport()
    sender = QzoneCommentReplySender(transport, owner_actor_uin=OWNER)
    asyncio.run(sender(target, "那我陪你"))
    assert transport.form["commentId"] == "1"
    assert transport.form["commentUin"] == OWNER
