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

    assert text == "今晚的风里有雨的味道。"
    assert primary.calls and backup.calls
    assert context.config_requests == [UMO]


def test_falls_back_when_primary_returns_empty_text():
    primary = FakeProvider("primary/model", text="   ")
    backup = FakeProvider("backup/model", text="路灯把影子拉得很长。")
    context = FakeContext("primary/model", {"primary/model": primary, "backup/model": backup}, ["backup/model"])

    text = asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert text == "路灯把影子拉得很长。"


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
    backup = FakeProvider("backup/model", text="夜色很安静。")
    context = FakeContext(
        "primary/model",
        {"primary/model": primary, "backup/model": backup},
        ["gone/model", "backup/model"],
    )

    text = asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    assert text == "夜色很安静。"


def test_provider_call_stays_history_free_and_umo_free():
    primary = FakeProvider("primary/model", text="今天也走了同一条路。")
    context = FakeContext("primary/model", {"primary/model": primary})

    asyncio.run(AstrBotTextGenerationAdapter(context).generate(_source(), umo=UMO))

    call = primary.calls[0]
    assert call["contexts"] == []
    assert call["system_prompt"] == TEXT_SYSTEM_PROMPT
    assert call["request_max_retries"] == 1
    assert UMO not in str(call["prompt"])
