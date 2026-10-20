"""Offline regression checks for Qzone image transport across processes."""

import asyncio
import base64

import pytest
from astrbot_plugin_qzone_dynamics.adapters import NapCatQzonePublisher
from astrbot_plugin_qzone_dynamics.core import DefinitelyNotDelivered


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **payload):
        self.calls.append((action, payload))
        return {"status": "ok", "data": {"tid": "test-tid"}}


@pytest.mark.parametrize("as_uri", [False, True])
def test_local_image_bytes_reach_client_without_path(tmp_path, as_uri):
    path = tmp_path / "配图 with space.png"
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    path.write_bytes(raw)
    client = FakeClient()
    publisher = NapCatQzonePublisher(client, image_enabled=True)
    payload = {
        "content": "午后的书。",
        "ugc_right": 4,
        "images": [path.as_uri() if as_uri else str(path)],
    }
    assert asyncio.run(publisher.send(payload, idempotency_key="test")) == "test-tid"
    action, sent = client.calls[0]
    assert action == "send_qzone_msg"
    assert sent["images"][0].startswith("base64://")
    assert base64.b64decode(sent["images"][0][9:]) == raw
    assert sent["ugc_right"] == 4
    assert payload["images"][0] != sent["images"][0]


@pytest.mark.parametrize("kind", ["missing", "empty", "oversize"])
def test_invalid_local_file_never_calls_remote(tmp_path, kind):
    path = tmp_path / "invalid.png"
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "oversize":
        with path.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    client = FakeClient()
    publisher = NapCatQzonePublisher(client, image_enabled=True)
    with pytest.raises(DefinitelyNotDelivered):
        asyncio.run(
            publisher.send(
                {"content": "午后的书。", "ugc_right": 4, "images": [path.as_uri()]},
                idempotency_key="test",
            )
        )
    assert client.calls == []


def test_text_only_publish_keeps_empty_images():
    client = FakeClient()
    publisher = NapCatQzonePublisher(client)
    asyncio.run(
        publisher.send(
            {"content": "午后的书。", "ugc_right": 4}, idempotency_key="test"
        )
    )
    assert client.calls[0][1]["images"] == []
