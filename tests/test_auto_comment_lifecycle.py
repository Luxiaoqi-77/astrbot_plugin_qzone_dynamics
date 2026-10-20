"""Plugin lifecycle and exact-scope gates, using fake tasks only."""

import asyncio

import pytest
from astrbot_plugin_qzone_dynamics.main import (
    AUTO_TARGET_UMO,
    COMMENT_BOT_ACCOUNT_UIN,
    COMMENT_OWNER_ACTOR_UIN,
    QzoneDynamicsPlugin,
)


def plugin():
    obj = QzoneDynamicsPlugin.__new__(QzoneDynamicsPlugin)
    obj.config = {
        "enabled": True,
        "comment_auto_reply_enabled": True,
        "authorized_user_qq": COMMENT_OWNER_ACTOR_UIN,
        "authorized_umo": AUTO_TARGET_UMO,
        "bot_account_uin": COMMENT_BOT_ACCOUNT_UIN,
    }
    return obj


@pytest.mark.parametrize(
    "key,value",
    [
        ("enabled", False),
        ("comment_auto_reply_enabled", False),
        ("authorized_user_qq", "999999999"),
        ("authorized_umo", "other"),
        ("bot_account_uin", "999999998"),
    ],
)
def test_scope_or_switch_mismatch_disables_automatic_reply(key, value):
    obj = plugin()
    assert obj._auto_comment_ready()
    obj.config[key] = value
    assert not obj._auto_comment_ready()


def test_initialize_once_and_terminate_cancels_comment_task():
    async def execute():
        obj = plugin()
        obj._stage_exact_historical_receipts = lambda: None
        obj._auto_publish_ready = lambda: False
        obj._auto_task = None
        obj._memory_sync_task = None
        obj._auto_comment_task = None
        started = asyncio.Event()

        async def idle():
            await asyncio.Event().wait()

        async def comment_loop():
            started.set()
            await asyncio.Event().wait()

        obj._memory_sync_loop = idle
        obj._auto_comment_loop = comment_loop
        await obj.initialize()
        await started.wait()
        task = obj._auto_comment_task
        await obj.initialize()
        assert obj._auto_comment_task is task
        await obj.terminate()
        assert task.cancelled()
        assert obj._auto_comment_task is None

    asyncio.run(execute())
