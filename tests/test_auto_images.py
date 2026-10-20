"""Offline tests for the auto-image quota and the image-publish payload."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot_plugin_qzone_dynamics.auto_images import AutoImageQuota
from astrbot_plugin_qzone_dynamics.core import (
    DraftStore,
    PublicationService,
    approve_draft,
    build_napcat_payload,
    confirmation_code_for_draft,
)
from astrbot_plugin_qzone_dynamics.image_assets import (
    build_qzone_image_request,
    qzone_image_subject,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
IMAGE_URI = "file:///tmp/qzone/image_assets/" + "a" * 64 + ".png"


def _quota(tmp_path: Path, probability: float = 0.45) -> AutoImageQuota:
    return AutoImageQuota(tmp_path / "auto_image_state.json", probability=probability)


def test_roll_is_persisted_and_does_not_reroll(tmp_path):
    quota = _quota(tmp_path, probability=1.0)
    assert quota.roll_for_day(NOW.date(), chooser=random.Random(1)) is True
    # Probability 0 would flip a fresh roll; the cached decision must win.
    assert quota.roll_for_day(NOW.date(), probability=0.0) is True


def test_roll_zero_probability_is_no(tmp_path):
    quota = _quota(tmp_path, probability=0.0)
    assert quota.roll_for_day(NOW.date(), chooser=random.Random(1)) is False


def test_reserve_is_busy_while_lease_is_active(tmp_path):
    quota = _quota(tmp_path)
    assert quota.reserve("t1", "d1", NOW)["status"] == "reserved"
    assert quota.reserve("t2", "d2", NOW + timedelta(minutes=5))["status"] == "busy"


def test_expired_generation_is_quarantined_not_retried(tmp_path):
    quota = _quota(tmp_path)
    assert quota.reserve("t1", "d1", NOW)["status"] == "reserved"
    later = NOW + timedelta(hours=2)
    assert quota.reserve("t2", "d2", later)["status"] == "unknown"
    # Unknown blocks the channel for 48 hours.
    assert quota.reserve("t3", "d3", later + timedelta(hours=47))["status"] == "unknown"
    assert (
        quota.reserve("t4", "d4", later + timedelta(hours=49))["status"] == "reserved"
    )


def test_success_starts_48_hour_cooldown(tmp_path):
    quota = _quota(tmp_path)
    quota.mark_success(NOW)
    assert quota.reserve("t1", "d1", NOW + timedelta(hours=47))["status"] == "cooldown"
    assert quota.reserve("t2", "d2", NOW + timedelta(hours=49))["status"] == "reserved"


def test_ready_reservation_blocks_then_expires(tmp_path):
    quota = _quota(tmp_path)
    quota.reserve("t1", "d1", NOW)
    assert quota.mark_ready("t1", IMAGE_URI, NOW) is True
    assert quota.reserve("t2", "d2", NOW + timedelta(minutes=10))["status"] == "busy"
    assert (
        quota.reserve("t3", "d3", NOW + timedelta(hours=2))["status"] == "reserved"
    )


def test_release_only_clears_matching_token(tmp_path):
    quota = _quota(tmp_path)
    quota.reserve("t1", "d1", NOW)
    quota.release("other")
    assert quota.status()["reservation"]["token"] == "t1"
    quota.release("t1")
    assert quota.status()["reservation"] == {}


def test_corrupted_state_is_reported_unavailable(tmp_path):
    path = tmp_path / "auto_image_state.json"
    path.write_text("{not json", encoding="utf-8")
    quota = _quota(tmp_path)
    try:
        quota.reserve("t1", "d1", NOW)
    except Exception as exc:  # noqa: BLE001 - the type is the assertion
        assert type(exc).__name__ == "AutoImageStoreUnavailable"
    else:  # pragma: no cover
        raise AssertionError("corrupted state must not be treated as empty")


def test_image_request_picks_one_concrete_subject():
    assert qzone_image_subject("今天在面馆坐了很久") == "一碗刚端上来的面"
    assert "一碗刚端上来的面" in build_qzone_image_request("今天在面馆坐了很久")
    ambiguous = build_qzone_image_request("窗外的夜色安静下来")
    assert "窗外的夜色" in ambiguous
    assert "选择" not in ambiguous


def test_payload_includes_file_uri_only_when_enabled():
    payload = build_napcat_payload(
        "普通动态",
        ugc_right=4,
        images=(IMAGE_URI,),
        image_enabled=True,
    )
    assert payload["images"] == [IMAGE_URI]
    assert payload["ugc_right"] == 4


def test_publish_once_sends_the_attached_image(tmp_path):
    store = DraftStore(tmp_path / "drafts.json")
    from astrbot_plugin_qzone_dynamics.core import SafeDraftInput

    source = SafeDraftInput(
        persona_id="p",
        source_date="2026-10-20",
        persona_label="小澄",
        activity="在协会整理报告",
    )
    draft, _ = store.create_or_update_source(source, "今天在协会整理报告。")
    draft = store.set_images(draft.draft_id, (IMAGE_URI,))
    approve_draft(store, draft.draft_id, ugc_right=4)

    sent: list[dict] = []

    class _Publisher:
        async def send(self, payload, *, idempotency_key):
            sent.append(payload)
            return "tid-1"

    service = PublicationService(store, _Publisher())
    reviewed = store.get(draft.draft_id)
    result = asyncio.run(
        service.publish_once(
            draft.draft_id,
            publish_enabled=True,
            ugc_right=4,
            confirmation_token=confirmation_code_for_draft(reviewed, 4, ()),
            images=reviewed.image_files,
            image_enabled=True,
        )
    )
    assert result.status == "published"
    assert sent and sent[0]["images"] == [IMAGE_URI]
