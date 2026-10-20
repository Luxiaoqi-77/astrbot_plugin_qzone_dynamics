"""Draft state, explicit review, and fail-closed publication policy."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
import uuid
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

ALLOWED_UGC_RIGHT = frozenset({1, 4, 16, 64, 128})
TARGETED_UGC_RIGHT = frozenset({16, 128})
MAX_PUBLISH_ATTEMPTS = 3
PUBLISH_RETRY_BASE_SECONDS = 30
PUBLISH_RETRY_MAX_SECONDS = 300
HEALTH_TERMS = (
    "健康",
    "病历",
    "生病",
    "就医",
    "症状",
    "诊断",
    "用药",
    "吃药",
    "医院",
    "看医生",
    "体检",
    "hospital",
    "diagnos",
    "medicat",
    "symptom",
    "health",
    "medical",
    "doctor",
    "clinic",
)


@dataclass(frozen=True)
class SafeDraftInput:
    persona_id: str
    source_date: str
    persona_label: str
    activity: str
    activity_status: str = ""
    activity_state: str = ""
    activity_interval: str = ""
    local_time: str = ""
    persona_public_note: str = ""
    outfit_summary: str = ""
    mood_label: str = ""
    mood_intensity: float | None = None
    mood_time: str = ""
    source_labels: tuple[str, ...] = ()
    primary_source_family: str = ""
    primary_event_fingerprint: str = ""
    primary_source_label: str = ""
    primary_source_context: str = ""
    source_reservation_token: str = ""
    primary_source_requires_review: bool = False
    primary_source_review_reason: str = ""
    # 2026-10-16：当天命中的"重要日子"（已按 qzone 渠道过滤、含侧重），供提示词注入。
    important_dates: tuple[str, ...] = ()

    @property
    def source_key(self) -> str:
        return f"{self.source_date}:{self.persona_id}"


@dataclass(frozen=True)
class Draft:
    draft_id: str
    content: str
    status: str = "pending"
    created_at: str = ""
    source_key: str = ""
    source_date: str = ""
    persona_id: str = ""
    approved_content_hash: str = ""
    approved_audience_hash: str = ""
    attempt_count: int = 0
    remote_tid: str = ""
    error_code: str = ""
    source_labels: tuple[str, ...] = ()
    source_fingerprint: str = ""
    memory_sync_state: str = ""
    published_at: str = ""
    material_family: str = ""
    material_event_fingerprint: str = ""
    material_reservation_token: str = ""
    material_requires_review: bool = False
    image_files: tuple[str, ...] = ()
    camera_image_state: str = "not_started"
    retry_after_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Draft:
        return cls(
            draft_id=str(value["draft_id"]),
            content=str(value["content"]),
            status=str(value.get("status", "pending")),
            created_at=str(value.get("created_at", "")),
            source_key=str(value.get("source_key", "")),
            source_date=str(value.get("source_date", "")),
            persona_id=str(value.get("persona_id", "")),
            approved_content_hash=str(value.get("approved_content_hash", "")),
            approved_audience_hash=str(value.get("approved_audience_hash", "")),
            attempt_count=int(value.get("attempt_count", 0)),
            remote_tid=str(value.get("remote_tid", "")),
            error_code=str(value.get("error_code", "")),
            source_labels=tuple(
                str(item)
                for item in value.get("source_labels", ())
                if isinstance(item, str)
            ),
            source_fingerprint=str(value.get("source_fingerprint", "")),
            memory_sync_state=str(value.get("memory_sync_state", "")),
            published_at=str(value.get("published_at", "")),
            material_family=str(value.get("material_family", "")),
            material_event_fingerprint=str(value.get("material_event_fingerprint", "")),
            material_reservation_token=str(value.get("material_reservation_token", "")),
            material_requires_review=bool(value.get("material_requires_review", False)),
            image_files=tuple(
                str(item) for item in value.get("image_files", ()) if isinstance(item, str)
            ),
            camera_image_state=str(value.get("camera_image_state", "not_started")),
            retry_after_at=str(value.get("retry_after_at", "")),
        )


class DraftLockedError(RuntimeError):
    """The draft has entered a send state and cannot be overwritten."""


class DraftStoreUnavailable(RuntimeError):
    """Persisted draft state is unreadable; creating or publishing must stop."""


def clean_source_text(
    value: object,
    label: str,
    *,
    required: bool = False,
    max_chars: int = 500,
) -> str:
    """Normalize a whitelisted text field and enforce a bounded size."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise ValueError(f"{label} is required")
    if len(cleaned) > max_chars:
        raise ValueError(f"{label} is too long")
    return cleaned


def _reject_health_terms(content: str) -> None:
    folded = content.casefold()
    if any(term.casefold() in folded for term in HEALTH_TERMS):
        raise ValueError("public draft contains a blocked health-related term")


def compose_qzone_draft(source: SafeDraftInput, *, include_mood: bool = False) -> str:
    """Compose text from a sanitized VirtualLife snapshot.

    Args:
        source: Persona, schedule, and outfit fields already filtered by the adapter.
        include_mood: Whether an optional public mood-state result is included.

    Returns:
        A short local-template draft.

    Raises:
        ValueError: If required fields are missing or content is disallowed.
    """
    persona = clean_source_text(source.persona_label, "persona label", required=True)
    activity = clean_source_text(source.activity, "schedule activity")
    public_note = clean_source_text(source.persona_public_note, "public persona note")
    mood = clean_source_text(source.mood_label, "public mood label", max_chars=40) if include_mood else ""
    _reject_health_terms(mood)
    del public_note, mood
    if activity:
        time_word = "现在" if source.activity_status == "正在进行" else "刚才"
        content = f"{persona}{time_word}{activity}。"
    elif source.primary_source_label:
        label = clean_source_text(source.primary_source_label, "primary source label", required=True, max_chars=80)
        content = f"{persona}今天想到{label}，心里多了点轻松的灵感。"
    else:
        content = f"{persona}今天想留意身边的一点小发现。"
    _reject_health_terms(content)
    if len(content) > 1200:
        raise ValueError("draft is too long")
    return content


def content_fingerprint(content: str, image_files: tuple[str, ...] | list[str] = ()) -> str:
    """Return a stable digest for the exact text and image assets a reviewer approved."""
    if not image_files:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {"content": content, "images": list(image_files)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_fingerprint(source: SafeDraftInput) -> str:
    """Fingerprint only the allowlisted source fields sent to draft generation."""
    source_payload = {
        "persona_id": source.persona_id,
        "source_date": source.source_date,
        "persona_label": source.persona_label,
        "activity": source.activity,
        "activity_status": source.activity_status,
        "activity_state": source.activity_state,
        "activity_interval": source.activity_interval,
        "persona_public_note": source.persona_public_note,
        "outfit_summary": source.outfit_summary,
        "mood_label": source.mood_label,
        "mood_intensity": source.mood_intensity,
        "source_labels": source.source_labels,
        "primary_source_family": source.primary_source_family,
        "primary_event_fingerprint": source.primary_event_fingerprint,
        "primary_source_label": source.primary_source_label,
        "primary_source_requires_review": source.primary_source_requires_review,
    }
    encoded = json.dumps(source_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DraftStore:
    """Persist review and publication state in the plugin-owned data directory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self) -> dict[str, Draft]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DraftStoreUnavailable("draft state is unreadable") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("drafts"), list):
            raise DraftStoreUnavailable("draft state has an invalid shape")
        drafts: dict[str, Draft] = {}
        for item in raw["drafts"]:
            if not isinstance(item, dict) or not item.get("draft_id"):
                raise DraftStoreUnavailable("draft state contains an invalid record")
            try:
                draft = Draft.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise DraftStoreUnavailable("draft state contains an invalid record") from exc
            drafts[draft.draft_id] = draft
        return drafts

    def _save(self, drafts: dict[str, Draft]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"schema_version": 4, "drafts": [asdict(item) for item in drafts.values()]}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def create_or_update_source(self, source: SafeDraftInput, content: str) -> tuple[Draft, str]:
        """Keep one draft per persona/day and reset review when its text changes."""
        with self.lock:
            drafts = self._load()
            current = next(
                (item for item in drafts.values() if item.source_key == source.source_key),
                None,
            )
            if current is None:
                created = Draft(
                    draft_id=uuid.uuid4().hex[:12],
                    content=content,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    source_key=source.source_key,
                    source_date=source.source_date,
                    persona_id=source.persona_id,
                    source_labels=source.source_labels,
                    source_fingerprint=source_fingerprint(source),
                    material_family=source.primary_source_family,
                    material_event_fingerprint=source.primary_event_fingerprint,
                    material_reservation_token=source.source_reservation_token,
                    material_requires_review=source.primary_source_requires_review,
                )
                drafts[created.draft_id] = created
                self._save(drafts)
                return created, "created"
            if current.status in {"sending", "delivery_unknown", "published"}:
                if (
                    current.content == content
                    and current.source_fingerprint == source_fingerprint(source)
                ):
                    return current, "unchanged"
                raise DraftLockedError(current.status)
            if current.content == content:
                fingerprint = source_fingerprint(source)
                if (
                    current.source_fingerprint != fingerprint
                    or current.source_labels != source.source_labels
                ):
                    current = Draft(
                        **{
                            **asdict(current),
                            "source_fingerprint": fingerprint,
                            "source_labels": source.source_labels,
                            "material_family": source.primary_source_family,
                            "material_event_fingerprint": source.primary_event_fingerprint,
                            "material_reservation_token": source.source_reservation_token,
                            "material_requires_review": source.primary_source_requires_review,
                        }
                    )
                    drafts[current.draft_id] = current
                    self._save(drafts)
                return current, "unchanged"
            updated = Draft(
                **{
                    **asdict(current),
                    "content": content,
                    "status": "pending",
                    "approved_content_hash": "",
                    "approved_audience_hash": "",
                    "error_code": "",
                    "remote_tid": "",
                    "retry_after_at": "",
                    "source_labels": source.source_labels,
                    "source_fingerprint": source_fingerprint(source),
                    "material_family": source.primary_source_family,
                    "material_event_fingerprint": source.primary_event_fingerprint,
                    "material_reservation_token": source.source_reservation_token,
                    "material_requires_review": source.primary_source_requires_review,
                }
            )
            drafts[current.draft_id] = updated
            self._save(drafts)
            return updated, "changed"

    def get(self, draft_id: str) -> Draft | None:
        with self.lock:
            return self._load().get(draft_id)

    def get_by_source(self, source_key: str) -> Draft | None:
        with self.lock:
            return next(
                (item for item in self._load().values() if item.source_key == source_key),
                None,
            )

    def set_images(
        self,
        draft_id: str,
        image_files: tuple[str, ...],
        *,
        camera_image_state: str | None = None,
    ) -> Draft:
        """Bind reviewed local image URIs to an editable draft and revoke prior approval."""
        if len(image_files) > 1 or any(not isinstance(item, str) for item in image_files):
            raise ValueError("one image URI or an empty image list is required")
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.status not in {"pending", "approved", "retryable_failed"}:
                raise DraftLockedError(current.status)
            if current.camera_image_state == "claimed":
                raise DraftLockedError("camera image generation is in progress")
            updated = Draft(
                **{
                    **asdict(current),
                    "image_files": tuple(image_files),
                    "status": "pending",
                    "approved_content_hash": "",
                    "approved_audience_hash": "",
                    "error_code": "image_set_changed_review_required",
                    "remote_tid": "",
                    "retry_after_at": "",
                    "camera_image_state": camera_image_state or current.camera_image_state,
                }
            )
            drafts[draft_id] = updated
            self._save(drafts)
            return updated

    def claim_camera_image_generation(self, draft_id: str) -> tuple[Draft, bool]:
        """Persist a one-shot image generation claim before any provider call."""
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.status not in {"pending", "approved", "retryable_failed"}:
                return current, False
            if current.image_files or current.camera_image_state != "not_started":
                return current, False
            claimed = Draft(
                **{
                    **asdict(current),
                    "camera_image_state": "claimed",
                    "status": "pending",
                    "approved_content_hash": "",
                    "approved_audience_hash": "",
                    "error_code": "camera_image_generation_claimed",
                }
            )
            drafts[draft_id] = claimed
            self._save(drafts)
            return claimed, True

    def finish_camera_image_generation(
        self, draft_id: str, image_uri: str, *, succeeded: bool
    ) -> Draft:
        """Record the single generation outcome; uncertain outcomes are never retried."""
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.camera_image_state != "claimed":
                raise DraftLockedError("camera image generation has no active claim")
            if succeeded:
                updated = Draft(
                    **{
                        **asdict(current),
                        "image_files": (image_uri,),
                        "camera_image_state": "ready",
                        "approved_content_hash": "",
                        "approved_audience_hash": "",
                        "error_code": "camera_image_ready_review_required",
                    }
                )
            else:
                updated = Draft(
                    **{
                        **asdict(current),
                        "camera_image_state": "unknown",
                        "error_code": "camera_image_outcome_unknown_no_retry",
                    }
                )
            drafts[draft_id] = updated
            self._save(drafts)
            return updated

    def list(self) -> list[Draft]:
        with self.lock:
            return list(self._load().values())

    def invalidate_stale_audience(
        self,
        draft_id: str,
        *,
        ugc_right: int | str | None,
        target_uins: tuple[str, ...] | list[str] = (),
    ) -> tuple[Draft, bool]:
        """Revoke a review when its persisted audience no longer matches config."""
        try:
            current_audience_hash = audience_fingerprint(ugc_right, target_uins)
        except (TypeError, ValueError):
            current_audience_hash = ""
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.status not in {"approved", "retryable_failed"}:
                return current, False
            if current_audience_hash and current.approved_audience_hash == current_audience_hash:
                return current, False
            reset = Draft(
                **{
                    **asdict(current),
                    "status": "pending",
                    "approved_content_hash": "",
                    "approved_audience_hash": "",
                    "error_code": "review_audience_changed",
                }
            )
            drafts[draft_id] = reset
            self._save(drafts)
            return reset, True

    def transition(
        self, draft_id: str, expected: set[str], status: str, **changes: object
    ) -> Draft:
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.status not in expected:
                return current
            updated = Draft(**{**asdict(current), "status": status, **changes})
            drafts[draft_id] = updated
            self._save(drafts)
            return updated

    def claim_for_publish(
        self, draft_id: str, *, audience_hash: str
    ) -> tuple[Draft, bool]:
        """Persist a one-time send claim only for the exact reviewed text."""
        with self.lock:
            drafts = self._load()
            current = drafts.get(draft_id)
            if current is None:
                raise KeyError(draft_id)
            if current.status not in {"approved", "retryable_failed"}:
                return current, False
            if (
                current.approved_content_hash
                != content_fingerprint(current.content, current.image_files)
                or current.approved_audience_hash != audience_hash
            ):
                reset = Draft(
                    **{
                        **asdict(current),
                        "status": "pending",
                        "approved_content_hash": "",
                        "approved_audience_hash": "",
                        "error_code": "review_digest_or_audience_mismatch",
                    }
                )
                drafts[draft_id] = reset
                self._save(drafts)
                return reset, False
            claimed = Draft(
                **{
                    **asdict(current),
                    "status": "sending",
                    "attempt_count": current.attempt_count + 1,
                    "error_code": "",
                    "retry_after_at": "",
                }
            )
            drafts[draft_id] = claimed
            self._save(drafts)
            return claimed, True


def approve_draft(
    store: DraftStore,
    draft_id: str,
    *,
    ugc_right: int | str | None,
    target_uins: tuple[str, ...] | list[str] = (),
) -> Draft:
    """Approve the exact content and explicit audience locally without publishing."""
    draft = store.get(draft_id)
    if draft is None:
        raise KeyError(draft_id)
    audience_hash = audience_fingerprint(ugc_right, target_uins)
    return store.transition(
        draft_id,
        {"pending", "approved", "retryable_failed"},
        "approved",
        approved_content_hash=content_fingerprint(draft.content, draft.image_files),
        approved_audience_hash=audience_hash,
        error_code="",
    )


def cancel_draft(store: DraftStore, draft_id: str) -> Draft:
    """Cancel an unsent draft."""
    return store.transition(
        draft_id, {"pending", "approved", "retryable_failed"}, "cancelled"
    )


def build_napcat_payload(
    content: str,
    *,
    ugc_right: int | str | None,
    target_uins: tuple[str, ...] | list[str] = (),
    images: tuple[str, ...] | list[str] = (),
    image_enabled: bool = False,
) -> dict[str, object]:
    """Build the locally verified NapCat payload without sending it.

    Args:
        content: Approved post text.
        ugc_right: Explicit viewer scope; there is no implicit public default.
        target_uins: Required audience list for scopes 16 and 128.
        images: Optional explicit image references.
        image_enabled: Must be true before images can be included.

    Returns:
        A payload for `send_qzone_msg`.

    Raises:
        ValueError: If the audience, target list, image switch, or text is invalid.
    """
    cleaned = clean_source_text(content, "content", required=True, max_chars=1200)
    _reject_health_terms(cleaned)
    if isinstance(ugc_right, bool):
        scope = -1
    elif isinstance(ugc_right, int):
        scope = ugc_right
    elif isinstance(ugc_right, str) and ugc_right.strip() in {
        str(value) for value in ALLOWED_UGC_RIGHT
    }:
        scope = int(ugc_right.strip())
    else:
        scope = -1
    if scope not in ALLOWED_UGC_RIGHT:
        raise ValueError("an explicit ugc_right of 1, 4, 16, 64, or 128 is required")
    if not isinstance(target_uins, (tuple, list)):
        raise ValueError("target_uins must be an explicit list")
    targets = [str(item).strip() for item in target_uins if str(item).strip()]
    if any(not re.fullmatch(r"\d{5,15}", item) for item in targets):
        raise ValueError("target_uins must contain valid numeric QQ UINs")
    if scope in TARGETED_UGC_RIGHT and not targets:
        raise ValueError("ugc_right 16/128 requires target_uins")
    if scope not in TARGETED_UGC_RIGHT and targets:
        raise ValueError("target_uins are only valid for ugc_right 16/128")
    if not isinstance(images, (tuple, list)):
        raise ValueError("images must be an explicit list")
    if images and not image_enabled:
        raise ValueError("image attachments are disabled")
    if any(not isinstance(item, str) for item in images):
        raise ValueError("image references must be text")
    image_values = [item.strip() for item in images if item.strip()]
    return {
        "content": cleaned,
        "images": image_values,
        "ugc_right": scope,
        "target_uins": targets,
    }


def audience_fingerprint(
    ugc_right: int | str | None,
    target_uins: tuple[str, ...] | list[str] = (),
) -> str:
    """Hash the normalized scope and target set that an administrator reviewed."""
    payload = build_napcat_payload(
        "audience review",
        ugc_right=ugc_right,
        target_uins=target_uins,
        image_enabled=False,
    )
    canonical_audience = {
        "ugc_right": payload["ugc_right"],
        "target_uins": sorted(set(payload["target_uins"])),
    }
    encoded = json.dumps(canonical_audience, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DefinitelyNotDelivered(RuntimeError):
    """Publisher confirms the request was not delivered and can be retried."""


class DeliveryOutcomeUnknown(RuntimeError):
    """Publisher cannot confirm whether the remote action was delivered."""


class Publisher(Protocol):
    def send(
        self, payload: dict[str, object], *, idempotency_key: str
    ) -> str | Awaitable[str]:
        """Send a payload and return the remote post ID."""


class PublicationService:
    """Apply review, audience, and no-duplicate rules to an injected publisher."""

    def __init__(self, store: DraftStore, publisher: Publisher):
        self.store = store
        self.publisher = publisher

    async def publish_once(
        self,
        draft_id: str,
        *,
        publish_enabled: bool,
        ugc_right: int | str | None,
        confirmation_token: str,
        target_uins: tuple[str, ...] | list[str] = (),
        images: tuple[str, ...] | list[str] = (),
        image_enabled: bool = False,
    ) -> Draft:
        """Publish one unchanged, reviewed draft through an injected adapter.

        Args:
            draft_id: Local draft identifier used as the stable idempotency key.
            publish_enabled: Explicit switch; false prevents publisher calls.
            ugc_right: Explicit audience scope; empty values are rejected.
            target_uins: Explicit target list for partial audience scopes.
            images: Optional references permitted only by image_enabled.
            image_enabled: Explicit image attachment switch.

        Returns:
            The latest persisted draft state.

        Raises:
            PermissionError: If publishing is disabled.
            KeyError: If the draft does not exist.
            ValueError: If the payload is invalid.
        """
        if not publish_enabled:
            raise PermissionError("publishing is disabled")
        current = self.store.get(draft_id)
        if current is None:
            raise KeyError(draft_id)
        current, audience_changed = self.store.invalidate_stale_audience(
            draft_id, ugc_right=ugc_right, target_uins=target_uins
        )
        if audience_changed:
            raise PermissionError("audience changed; prior review was revoked")
        if current.status in {"sending", "delivery_unknown", "published"}:
            return current
        if current.error_code == "definitely_not_delivered":
            if current.attempt_count >= MAX_PUBLISH_ATTEMPTS:
                if current.status == "retryable_failed":
                    return self.store.transition(
                        draft_id,
                        {"retryable_failed"},
                        "failed",
                        error_code="confirmed_failure_attempt_limit",
                        retry_after_at="",
                    )
                return current
            try:
                retry_at = datetime.fromisoformat(current.retry_after_at)
            except (TypeError, ValueError):
                return current
            if (
                not current.retry_after_at
                or retry_at.tzinfo is None
                or datetime.now(timezone.utc) < retry_at
            ):
                return current
        if current.status not in {"approved", "retryable_failed"}:
            raise PermissionError("this draft is not approved for explicit publishing")
        expected_confirmation = confirmation_code_for_draft(
            current, ugc_right, target_uins
        )
        if not expected_confirmation or confirmation_token != expected_confirmation:
            raise PermissionError("per-draft content confirmation is missing or stale")
        payload = build_napcat_payload(
            current.content,
            ugc_right=ugc_right,
            target_uins=target_uins,
            images=current.image_files,
            image_enabled=image_enabled,
        )
        current_audience_hash = audience_fingerprint(ugc_right, target_uins)
        claimed, did_claim = self.store.claim_for_publish(
            draft_id, audience_hash=current_audience_hash
        )
        if not did_claim:
            return claimed
        try:
            result = self.publisher.send(payload, idempotency_key=draft_id)
            tid = await result if inspect.isawaitable(result) else result
            if not isinstance(tid, str) or not tid.strip():
                raise DeliveryOutcomeUnknown("publisher returned no tid")
        except DefinitelyNotDelivered:
            delay = min(
                PUBLISH_RETRY_MAX_SECONDS,
                PUBLISH_RETRY_BASE_SECONDS * (2 ** max(claimed.attempt_count - 1, 0)),
            )
            retryable = claimed.attempt_count < MAX_PUBLISH_ATTEMPTS
            return self.store.transition(
                draft_id,
                {"sending"},
                "retryable_failed" if retryable else "failed",
                error_code="definitely_not_delivered",
                retry_after_at=(
                    (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    if retryable
                    else ""
                ),
            )
        except DeliveryOutcomeUnknown:
            return self.store.transition(
                draft_id,
                {"sending"},
                "delivery_unknown",
                error_code="delivery_outcome_unknown",
            )
        except Exception as exc:
            return self.store.transition(
                draft_id,
                {"sending"},
                "delivery_unknown",
                error_code=type(exc).__name__,
            )
        return self.store.transition(
            draft_id,
            {"sending"},
            "published",
            remote_tid=str(tid),
            memory_sync_state="pending",
            published_at=datetime.now(timezone.utc).isoformat(),
            retry_after_at="",
        )


def confirmation_code_for_draft(
    draft: Draft,
    ugc_right: int | str | None = None,
    target_uins: tuple[str, ...] | list[str] = (),
) -> str:
    """Return a token only when the exact reviewed body and audience still match."""
    if (
        draft.status not in {"approved", "retryable_failed"}
        or draft.approved_content_hash
        != content_fingerprint(draft.content, draft.image_files)
    ):
        return ""
    try:
        current_audience_hash = audience_fingerprint(ugc_right, target_uins)
    except (TypeError, ValueError):
        return ""
    if draft.approved_audience_hash != current_audience_hash:
        return ""
    confirmation_digest = hashlib.sha256(
        f"{draft.approved_content_hash}\0{current_audience_hash}".encode()
    ).hexdigest()
    return f"确认-{confirmation_digest[:8]}"
