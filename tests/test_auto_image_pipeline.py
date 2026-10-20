"""Offline integration tests for the auto-image preparation path.

The camera generator is replaced with a stub that writes a real (tiny) PNG, so
the staging, hashing, quota and fallback logic run end to end without any paid
model call or QQ interaction.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_qzone_dynamics.auto_images import AutoImageQuota
from astrbot_plugin_qzone_dynamics.image_assets import verify_staged_image
from astrbot_plugin_qzone_dynamics.main import QzoneDynamicsPlugin

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"offline-test-image" * 8
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)


class _StubCamera:
    """Writes one PNG into a temp "camera" directory."""

    def __init__(self, image_dir: Path, *, fail: bool = False):
        self.image_dir = image_dir
        self.fail = fail
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def generate_moment_file(self, want: str, persona_context: str = ""):
        if self.fail:
            raise RuntimeError("stub camera failure")
        path = self.image_dir / "xhc_20261020_040000_000000.png"
        path.write_bytes(PNG_BYTES)
        return path


def _plugin(tmp_path: Path, camera) -> QzoneDynamicsPlugin:
    plugin = object.__new__(QzoneDynamicsPlugin)
    plugin.config = {
        "image_enabled": True,
        "auto_publish_image_enabled": True,
        "auto_publish_image_probability": 1.0,
    }
    plugin._image_asset_dir = tmp_path / "image_assets"
    plugin._auto_image_quota = AutoImageQuota(
        tmp_path / "auto_image_state.json", probability=1.0
    )
    plugin._get_camera_generator = lambda: camera  # type: ignore[method-assign]
    return plugin


def _draft() -> SimpleNamespace:
    return SimpleNamespace(draft_id="draft-1", content="今天在面馆坐了很久，汤头有点咸。")


def test_prepare_auto_image_stages_and_claims(tmp_path):
    camera = _StubCamera(tmp_path / "camera")
    plugin = _plugin(tmp_path, camera)

    uri, token = asyncio.run(plugin._prepare_auto_image(_draft(), NOW))

    assert token and uri.startswith("file://")
    assert verify_staged_image(uri, plugin._image_asset_dir)
    status = plugin._auto_image_quota.status()
    assert status["reservation"]["state"] == "ready"
    assert status["reservation"]["asset_uri"] == uri


def test_second_prepare_same_day_falls_back_text_only(tmp_path):
    camera = _StubCamera(tmp_path / "camera")
    plugin = _plugin(tmp_path, camera)
    first_uri, _ = asyncio.run(plugin._prepare_auto_image(_draft(), NOW))
    assert first_uri
    # Same day cached "yes", but the ready claim is still leased.
    second_uri, second_token = asyncio.run(
        plugin._prepare_auto_image(_draft(), NOW)
    )
    assert second_uri == "" and second_token == ""


def test_camera_failure_quarantines_without_retry(tmp_path):
    camera = _StubCamera(tmp_path / "camera", fail=True)
    plugin = _plugin(tmp_path, camera)

    uri, token = asyncio.run(plugin._prepare_auto_image(_draft(), NOW))

    assert uri == "" and token == ""
    status = plugin._auto_image_quota.status()
    assert status["unknown_since"]
    assert status["reservation"] == {}
    # 48 hours of unknown: the channel stays closed.
    blocked, _ = asyncio.run(
        plugin._prepare_auto_image(_draft(), NOW.replace(hour=8))
    )
    assert blocked == ""


def test_image_master_switch_off_disables_auto_images(tmp_path):
    camera = _StubCamera(tmp_path / "camera")
    plugin = _plugin(tmp_path, camera)
    plugin.config["image_enabled"] = False
    assert plugin._auto_image_allowed() is False
    assert plugin._auto_image_allowed.__self__ is plugin
