"""Offline tests for provider resolution and fallback in the text adapter.

The failure this guards against: the UMO's primary chat provider pointed at an
upstream channel that answered `503 model_not_found`. The adapter used to stop
there and the owner only saw a generic "文字 provider 请求失败". It must now walk
the configured AstrBot fallback providers before giving up, and still send no
chat history, UMO, or tools to any of them.
"""

from __future__ import annotations

import asyncio

import pytest
from astrbot_plugin_qzone_dynamics.adapters import (
    TEXT_SYSTEM_PROMPT,
    AstrBotTextGenerationAdapter,
    InvalidGeneratedDraft,
    TextProviderUnavailable,
)
from astrbot_plugin_qzone_dynamics.core import SafeDraftInput

UMO = "qq_napcat_100000002:FriendMessage:100000001"


class _Meta:
    provider_type = "chat_completion"


class _Response:
    def __init__(self, text: str):
        self.completion_text = text


class FakeProvider:
    """Minimal chat provider that records the requests it received."""

    def __init__(self, provider_id: str, *, text: str = "", error: Exception | None = None):
        self.provider_id = provider_id
        self._text = text
        self._error = error
        self.calls: list[dict[str, object]] = []

    def meta(self):
        return _Meta()

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return _Response(self._text)


class FakeContext:
    def __init__(self, primary_id: str, providers: dict[str, object], fallbacks=()):
        self.primary_id = primary_id
        self.providers = providers
        self.fallbacks = list(fallbacks)
        self.config_requests: list[str] = []

    async def get_current_chat_provider_id(self, umo: str) -> str:
        assert umo == UMO
        return self.primary_id

    def get_provider_by_id(self, provider_id: str):
        return self.providers.get(provider_id)

    def get_config(self, umo: str | None = None):
        self.config_requests.append(str(umo))
        return {
            "agent_runner": {
                "runner_type": "local",
                "config": {"model": {"fallback_provider_ids": list(self.fallbacks)}},
            }
        }


def _source() -> SafeDraftInput:
    return SafeDraftInput(
        persona_id="shenxinghui",
        source_date="2026-10-20",
        persona_label="沈星回",
        persona_public_note="公开角色设定：喜欢在夜里散步，说话温和。",
        activity="在城市里散步",
        activity_status="正在进行",
        activity_state="available",
        local_time="21:30",
    )


def test_falls_back_when_primary_channel_is_down():
    primary = FakeProvider("primary/model", error=RuntimeError("503 model_not_found"))
    backup = FakeProvider("backup/model", text="今晚的风里有雨的味道。")
    context = FakeContext("primary/model", {"primary/model": primary, "backup/model": backup}, ["backup/model"])

    text = asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert text == "今晚的风里有雨的味道"
    assert primary.calls and backup.calls
    assert context.config_requests == [UMO]


def test_falls_back_when_primary_returns_empty_text():
    primary = FakeProvider("primary/model", text="   ")
    backup = FakeProvider("backup/model", text="路灯把我的影子拉得很长。")
    context = FakeContext("primary/model", {"primary/model": primary, "backup/model": backup}, ["backup/model"])

    text = asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert text == "路灯把我的影子拉得很长"


def test_raises_content_error_when_every_candidate_returns_empty():
    primary = FakeProvider("primary/model", text="")
    backup = FakeProvider("backup/model", text="")
    context = FakeContext("primary/model", {"primary/model": primary, "backup/model": backup}, ["backup/model"])

    with pytest.raises(InvalidGeneratedDraft):
        asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))


def test_reports_last_provider_error_when_every_candidate_fails():
    primary = FakeProvider("primary/model", error=RuntimeError("503 model_not_found"))
    backup = FakeProvider("backup/model", error=TimeoutError("upstream timeout"))
    context = FakeContext("primary/model", {"primary/model": primary, "backup/model": backup}, ["backup/model"])

    with pytest.raises(TextProviderUnavailable) as excinfo:
        asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert "TimeoutError" in str(excinfo.value)


def test_missing_fallback_provider_is_skipped():
    primary = FakeProvider("primary/model", error=RuntimeError("503"))
    backup = FakeProvider("backup/model", text="今晚的夜色格外地安静。")
    context = FakeContext(
        "primary/model",
        {"primary/model": primary, "backup/model": backup},
        ["gone/model", "backup/model"],
    )

    text = asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert text == "今晚的夜色格外地安静"


def test_provider_call_stays_history_free_and_umo_free():
    primary = FakeProvider("primary/model", text="今天也走了平时同一条路。")
    context = FakeContext("primary/model", {"primary/model": primary})

    asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    call = primary.calls[0]
    assert call["contexts"] == []
    assert call["system_prompt"].endswith("\n\n" + TEXT_SYSTEM_PROMPT)
    assert "Asia/Shanghai（UTC+08:00）" in call["system_prompt"]
    assert call["request_max_retries"] == 1
    assert UMO not in str(call["prompt"])

def test_clean_source_text_drops_wakeup_schedule_tags() -> None:
    """说说文案同样不能带 wakeup 的 [NEXT: Xm]（模型会从历史里学）。"""

    from astrbot_plugin_qzone_dynamics.core import clean_source_text

    assert clean_source_text("今天风很大，早点回家[NEXT: 45m]", "draft") == "今天风很大，早点回家"
    assert clean_source_text("晚安【NEXT：8h】", "draft") == "晚安"
    # 没有标签时不改变原文（含普通方括号）。
    assert clean_source_text("下次再说[笑]", "draft") == "下次再说[笑]"


@pytest.mark.parametrize("important", [False, True])
@pytest.mark.parametrize("length", [9, 10, 20, 40, 41, 200, 201])
def test_generated_post_length_boundaries(length, important):
    provider = FakeProvider("primary/model", text="好" * length)
    adapter = AstrBotTextGenerationAdapter(FakeContext("primary/model", {"primary/model": provider}))
    from dataclasses import replace

    source = replace(_source(), important_dates=("纪念日",) if important else ())
    if 10 <= length <= (200 if important else 40):
        assert asyncio.run(adapter.generate(source, umo=UMO)) == "好" * length
    else:
        with pytest.raises(InvalidGeneratedDraft):
            asyncio.run(adapter.generate(source, umo=UMO))
