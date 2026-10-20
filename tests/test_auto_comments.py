"""Offline tests: no Qzone writes or model requests."""

import asyncio
import copy

import pytest
from astrbot_plugin_qzone_dynamics.auto_comments import AutoCommentReplies
from astrbot_plugin_qzone_dynamics.comment_reply import CommentReplyReceipts

BOT = "100000002"
OWNER = "100000001"


def thread(rows=()):
    return {
        "account_uin": BOT,
        "post_id": "post1",
        "post_author_uin": BOT,
        "post_content": "今天在看书",
        "comments": list(rows),
        "declared_comment_count": len(rows),
        "server_count_evidence": {"detail.totalCommentCount": len(rows)},
    }


def row(id="1", author=OWNER, parent=""):
    return {
        "comment_id": id,
        "author_uin": author,
        "content": "看什么书",
        "parent_comment_id": parent,
    }


class Workflow:
    def __init__(self, path, *, text="一起看吗？", result=None):
        self.snapshot = thread()
        self.receipts = CommentReplyReceipts(path / "receipts.json")
        self.generated = []
        self.sent = []
        self.on_sent = None
        self.text = text
        self.result = result or {"code": 0, "ret": 0}
        self.mutate = False

    async def thread_loader(self, account, post):
        if self.mutate and self.generated:
            self.snapshot["comments"][0]["content"] = "换了内容"
        return copy.deepcopy(self.snapshot)

    async def draft_generator(self, comment, content, *, thread_context=""):
        self.generated.append(comment.comment_id)
        self.last_thread_context = thread_context
        return self.text

    async def sender(self, target, content):
        self.sent.append((target, content))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def setup(tmp_path, **kwargs):
    service = AutoCommentReplies(
        tmp_path / "state.json", account_uin=BOT, owner_uin=OWNER
    )
    workflow = Workflow(tmp_path, **kwargs)
    return service, workflow


def run(service, workflow, **kwargs):
    return asyncio.run(service.poll_post(workflow, "post1", **kwargs))


def test_baseline_and_restart_do_not_reply_old_comments(tmp_path):
    service, workflow = setup(tmp_path)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "baseline"
    service = AutoCommentReplies(service.path, account_uin=BOT, owner_uin=OWNER)
    assert run(service, workflow) == "idle"
    assert workflow.generated == workflow.sent == []


def test_new_owner_comment_sent_once_across_restart(tmp_path):
    service, workflow = setup(tmp_path)
    assert run(service, workflow) == "baseline"
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "sent"
    assert len(workflow.sent) == 1
    service = AutoCommentReplies(service.path, account_uin=BOT, owner_uin=OWNER)
    assert run(service, workflow) == "idle"
    assert len(workflow.generated) == len(workflow.sent) == 1
    assert "看什么书" not in service.path.read_text()


@pytest.mark.parametrize(
    "result", [RuntimeError("timeout"), {"code": 0}, {"ret": -1, "code": 0}]
)
def test_unknown_outcome_never_retries(tmp_path, result):
    service, workflow = setup(tmp_path, result=result)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "delivery_unknown"
    assert run(service, workflow) == "idle"
    assert len(workflow.sent) == len(workflow.generated) == 1


@pytest.mark.parametrize("text", ["", "好" * 21])
def test_invalid_length_is_not_sent_or_regenerated(tmp_path, text):
    service, workflow = setup(tmp_path, text=text)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "generation_or_recheck_failed"
    assert run(service, workflow) == "idle"
    assert len(workflow.generated) == 1
    assert not workflow.sent


def test_exact_twenty_characters_allowed(tmp_path):
    service, workflow = setup(tmp_path, text="好" * 20)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "sent"


@pytest.mark.parametrize(
    "rows", [[row(author="100000003")], [row(), row("2", BOT, "1")]]
)
def test_other_people_and_already_replied_comments_skipped(tmp_path, rows):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread(rows)
    assert run(service, workflow) == "idle"
    assert workflow.generated == workflow.sent == []


def test_changed_thread_blocks_send(tmp_path):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    workflow.mutate = True
    assert run(service, workflow) == "generation_or_recheck_failed"
    assert not workflow.sent


def test_incomplete_thread_does_not_create_baseline(tmp_path):
    service, workflow = setup(tmp_path)
    workflow.snapshot["declared_comment_count"] = 1
    with pytest.raises(ValueError):
        run(service, workflow)
    assert not service.path.exists()


def test_disabled_does_not_read_or_generate(tmp_path):
    service, workflow = setup(tmp_path)
    assert run(service, workflow, ready=lambda: False) == "disabled"
    assert not service.path.exists()


def test_disable_after_generation_stops_send(tmp_path):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert (
        run(service, workflow, ready=lambda: not workflow.generated)
        == "disabled_before_send"
    )
    assert not workflow.sent


def test_existing_receipt_stops_regeneration(tmp_path):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "sent"
    service.path.unlink()  # Rebaseline without deleting the durable send receipt.
    workflow.snapshot = thread()
    run(service, workflow)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "idle"
    assert len(workflow.generated) == len(workflow.sent) == 1


def test_wrong_account_blocks_baseline(tmp_path):
    service, workflow = setup(tmp_path)
    workflow.snapshot["account_uin"] = "100000003"
    with pytest.raises(ValueError):
        run(service, workflow)


def test_cancelled_send_keeps_permanent_unknown_receipt(tmp_path):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread([row()])

    async def cancelled_sender(target, content):
        workflow.sent.append((target, content))
        raise asyncio.CancelledError

    workflow.sender = cancelled_sender
    with pytest.raises(asyncio.CancelledError):
        run(service, workflow)
    assert run(service, workflow) == "idle"
    assert len(workflow.sent) == 1
    assert "delivery_unknown" in workflow.receipts.path.read_text()


def test_concurrent_poll_only_generates_and_sends_once(tmp_path):
    service, workflow = setup(tmp_path)
    run(service, workflow)
    workflow.snapshot = thread([row()])

    async def concurrent():
        return await asyncio.gather(
            service.poll_post(workflow, "post1"), service.poll_post(workflow, "post1")
        )

    assert sorted(asyncio.run(concurrent())) == ["idle", "sent"]
    assert len(workflow.generated) == len(workflow.sent) == 1


def test_nested_owner_followup_gets_its_own_reply(tmp_path):
    """主人回复小回的回复时，也要单独触发一次自动回复（2026-10-20）。"""
    service, workflow = setup(tmp_path)
    workflow.snapshot = thread([row()])
    assert run(service, workflow) == "baseline"

    # 小回先回复了主评论，主人又在楼内追问。
    workflow.snapshot = thread(
        [
            row("1", OWNER, ""),
            row("2", BOT, "1"),
            row("3", OWNER, "2"),
        ]
    )
    workflow.text = "在楼下等你"
    assert run(service, workflow) == "sent"
    assert workflow.generated == ["3"]
    target, content = workflow.sent[0]
    assert target.parent_comment_id == "3"
    assert content == "在楼下等你"


def test_nested_followup_with_existing_bot_reply_is_skipped(tmp_path):
    service, workflow = setup(tmp_path)
    workflow.snapshot = thread([row("1", OWNER, "")])
    run(service, workflow)
    workflow.snapshot = thread(
        [
            row("1", OWNER, ""),
            row("2", BOT, "1"),
            row("3", OWNER, "2"),
            row("4", BOT, "3"),
        ]
    )
    assert run(service, workflow) == "idle"
    assert workflow.generated == workflow.sent == []


def test_thread_context_contains_reply_chain(tmp_path):
    service, workflow = setup(tmp_path)
    workflow.snapshot = thread([row("1", OWNER, "")])
    run(service, workflow)
    workflow.snapshot = thread(
        [
            row("1", OWNER, ""),
            row("2", BOT, "1"),
            row("3", OWNER, "2"),
        ]
    )
    assert run(service, workflow) == "sent"
    context = workflow.last_thread_context
    assert "主人: 看什么书" in context
    assert "你（小回）: 看什么书" in context


def test_readback_confirms_delivery_when_response_shape_is_unknown(tmp_path, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("astrbot_plugin_qzone_dynamics.auto_comments.asyncio.sleep", _no_sleep)
    service, workflow = setup(tmp_path, result={"code": 0, "ret": "ok"})
    workflow.snapshot = thread([row("1", OWNER, "")])
    run(service, workflow)

    workflow.snapshot = thread(
        [row("1", OWNER, ""), row("2", BOT, "1"), row("3", OWNER, "2")]
    )
    workflow.text = "那我陪你"
    # 发送后线程里出现小回的回复 → 回读确认为 sent。
    original_loader = workflow.thread_loader

    async def loader(account, post):
        snapshot = await original_loader(account, post)
        snapshot["declared_comment_count"] = len(snapshot["comments"]) + 1
        snapshot["server_count_evidence"] = {
            "detail.totalCommentCount": len(snapshot["comments"]) + 1
        }
        snapshot["comments"].append(
            {
                "comment_id": "4",
                "author_uin": BOT,
                "content": "那我陪你",
                "parent_comment_id": "1",
            }
        )
        return snapshot

    workflow.thread_loader = loader
    assert run(service, workflow) == "sent"


def test_readback_without_the_reply_keeps_unknown(tmp_path, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("astrbot_plugin_qzone_dynamics.auto_comments.asyncio.sleep", _no_sleep)
    service, workflow = setup(tmp_path, result={"code": 0, "ret": "ok"})
    workflow.snapshot = thread([row("1", OWNER, "")])
    run(service, workflow)
    workflow.snapshot = thread(
        [row("1", OWNER, ""), row("2", BOT, "1"), row("3", OWNER, "2")]
    )
    assert run(service, workflow) == "delivery_unknown"
    assert len(workflow.sent) == 1
