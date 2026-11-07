"""Native reply targets: synthetic threads and transport only, no QQ/model calls."""

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_qzone_dynamics.comment_reader import (
    QzoneCommentReplySender,
    flatten_detail_comments,
)
from astrbot_plugin_qzone_dynamics.comment_reply import (
    bind_reply_target,
    build_reply_content,
    matches_reply_content,
)
from astrbot_plugin_qzone_dynamics.comment_workflow import normalize_comment_thread

BOT = "100000002"
OWNER = "100000001"


def target_for(*, root_uin=OWNER, nested=False, nickname="测试昵称"):
    comments = [{"tid": "1", "uin": root_uin, "name": nickname, "content": "主评论"}]
    if nested:
        comments[0]["list_3"] = [
            {"tid": "2", "uin": OWNER, "name": nickname, "content": "追问"}
        ]
    rows = flatten_detail_comments(comments)
    snapshot = normalize_comment_thread({
        "account_uin": BOT, "post_author_uin": BOT, "post_id": "post1",
        "post_content": "动态", "comments": rows, "declared_comment_count": len(rows),
        "server_count_evidence": {"detail.totalCommentCount": len(rows)},
    })
    return bind_reply_target(
        account_uin=BOT, post_id="post1", post_author_uin=BOT,
        post_content="动态", comment_id="2" if nested else "1",
        expected_target_uin=OWNER, declared_comment_count=len(rows),
        comments=snapshot["comments"], comment_parent_id="1" if nested else "",
    )


class Transport:
    enabled = True
    credential_store = SimpleNamespace(account_uin=BOT)

    async def request(self, endpoint_key, **kwargs):
        self.endpoint = endpoint_key
        self.form = kwargs["data"]
        return b'{"code":0,"ret":0}'


@pytest.mark.parametrize("root_uin,nested", [(OWNER, False), (OWNER, True), (BOT, True)])
def test_full_thread_to_sender_keeps_floor_and_display_target_separate(root_uin, nested):
    target = target_for(root_uin=root_uin, nested=nested)
    transport = Transport()
    asyncio.run(QzoneCommentReplySender(transport, owner_actor_uin=OWNER)(target, "回评正文"))
    assert transport.endpoint == "reply_comment"
    assert transport.form["commentId"] == "1"
    assert transport.form["commentUin"] == root_uin
    assert transport.form["content"] == f"@{{uin:{OWNER},nick:测试昵称,who:1,auto:1}}回评正文"
    assert matches_reply_content(transport.form["content"], OWNER, "回评正文")


@pytest.mark.parametrize("nickname", ["", "含,逗号}和{括号", "姓名\n换行"])
def test_missing_or_delimited_nickname_cannot_break_target(nickname):
    content = build_reply_content("回评正文", OWNER, nickname)
    assert matches_reply_content(content, OWNER, "回评正文")
    assert content.count("@{") == content.count("}") == 1
    if not nickname:
        assert f"nick:{OWNER}," in content


@pytest.mark.parametrize("content", [
    "回评正文",
    f"@{{uin:{BOT},nick:别的对象,who:1,auto:1}}回评正文",
    f"@{{uin:{OWNER},nick:测试昵称,who:1,auto:1}}别的正文",
])
def test_same_floor_or_body_is_insufficient_for_reply_target(content):
    assert not matches_reply_content(content, OWNER, "回评正文")


def test_nickname_changes_preserve_existing_claim_identity_and_fingerprint():
    first = target_for(nickname="旧昵称")
    second = target_for(nickname="新昵称")
    assert first.idempotency_key == second.idempotency_key
    assert first.legacy_idempotency_key == second.legacy_idempotency_key
    assert first.thread_fingerprint == second.thread_fingerprint
