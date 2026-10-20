"""Durable 48-hour quota and daily dice roll for auto-published Qzone images.

The auto publisher may attach at most one camera image per 48 hours. The state
is persisted before the paid camera call so a crash can never silently pay for
a second image: an expired ``generating`` claim is quarantined as ``unknown``
instead of being retried.
"""

from __future__ import annotations

import json
import os
import random
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

COOLDOWN_SECONDS = 48 * 60 * 60
UNKNOWN_BLOCK_SECONDS = 48 * 60 * 60
DEFAULT_LEASE_SECONDS = 60 * 60
MAX_ROLL_DAYS = 7
DEFAULT_IMAGE_PROBABILITY = 0.45


class AutoImageStoreUnavailable(RuntimeError):
    """Persisted auto-image state is unreadable; image work must stop."""


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AutoImageQuota:
    """One image per 48 hours, one-shot claims, and a once-per-day dice roll."""

    def __init__(self, path: Path, *, probability: float = DEFAULT_IMAGE_PROBABILITY):
        self.path = Path(path)
        self.probability = probability
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ state
    def _defaults(self) -> dict:
        return {
            "schema_version": 1,
            "last_success_at": "",
            "unknown_since": "",
            "reservation": {},
            "rolls": {},
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._defaults()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AutoImageStoreUnavailable("auto image state is unreadable") from exc
        if not isinstance(raw, dict):
            raise AutoImageStoreUnavailable("auto image state has an invalid shape")
        state = self._defaults()
        state["last_success_at"] = str(raw.get("last_success_at", "") or "")
        state["unknown_since"] = str(raw.get("unknown_since", "") or "")
        reservation = raw.get("reservation")
        if reservation is not None and not isinstance(reservation, dict):
            raise AutoImageStoreUnavailable("auto image reservation is invalid")
        state["reservation"] = reservation or {}
        rolls = raw.get("rolls")
        if rolls is not None and not isinstance(rolls, dict):
            raise AutoImageStoreUnavailable("auto image rolls are invalid")
        state["rolls"] = {
            str(day): str(value)
            for day, value in (rolls or {}).items()
            if isinstance(day, str) and isinstance(value, str)
        }
        return state

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(temporary, self.path)

    @staticmethod
    def _within(value: str, now: datetime, seconds: int) -> bool:
        parsed = _parse_time(value)
        if parsed is None:
            return False
        return (now - parsed).total_seconds() < seconds

    # ------------------------------------------------------------------ roll
    def roll_for_day(
        self,
        day: date,
        *,
        probability: float | None = None,
        chooser: random.Random | None = None,
    ) -> bool:
        """Return today's image dice result, persisting it so retries do not reroll."""
        key = day.isoformat()
        chance = self.probability if probability is None else probability
        with self.lock:
            state = self._load()
            cached = state["rolls"].get(key)
            if cached in {"yes", "no"}:
                return cached == "yes"
            roll = chooser.random() if chooser is not None else random.random()
            decision = "yes" if roll < chance else "no"
            state["rolls"][key] = decision
            # Keep the ledger small: only recent days matter.
            keep = sorted(state["rolls"])[-MAX_ROLL_DAYS:]
            state["rolls"] = {item: state["rolls"][item] for item in keep}
            try:
                self._save(state)
            except OSError:
                # The decision still applies for this run; retrying is harmless
                # only because the caller never rerolls within one tick.
                pass
            return decision == "yes"

    # ------------------------------------------------------------ reservation
    def reserve(
        self,
        token: str,
        draft_id: str,
        now: datetime,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict:
        """Claim the image slot before calling the paid camera generator."""
        if not token or not 60 <= lease_seconds <= 7200:
            return {"status": "invalid"}
        now = now.astimezone(timezone.utc)
        with self.lock:
            state = self._load()
            if self._within(state["last_success_at"], now, COOLDOWN_SECONDS):
                return {"status": "cooldown"}
            if self._within(state["unknown_since"], now, UNKNOWN_BLOCK_SECONDS):
                return {"status": "unknown"}
            reservation = state["reservation"] or {}
            state_name = str(reservation.get("state", "") or "")
            expiry = _parse_time(reservation.get("expires_at"))
            active = expiry is not None and expiry > now
            if state_name == "generating":
                if active:
                    return {"status": "busy"}
                # A crash or lost result: never call the paid generator again.
                state["unknown_since"] = now.isoformat()
                state["reservation"] = {}
                self._save(state)
                return {"status": "unknown"}
            if state_name == "ready":
                if active:
                    return {"status": "busy"}
                # A ready image left over from an earlier slot cannot be
                # matched to a live draft safely; drop it and start fresh.
                state["reservation"] = {}
            expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            state["reservation"] = {
                "token": token,
                "draft_id": str(draft_id),
                "state": "generating",
                "asset_uri": "",
                "expires_at": expires_at,
            }
            self._save(state)
            return {"status": "reserved", "token": token}

    def mark_ready(self, token: str, asset_uri: str, now: datetime | None = None) -> bool:
        """Record that the camera produced the claimed image."""
        with self.lock:
            state = self._load()
            reservation = state["reservation"] or {}
            if reservation.get("token") != token or reservation.get("state") != "generating":
                return False
            moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            reservation["state"] = "ready"
            reservation["asset_uri"] = str(asset_uri)
            reservation["expires_at"] = (
                moment + timedelta(seconds=DEFAULT_LEASE_SECONDS)
            ).isoformat()
            state["reservation"] = reservation
            self._save(state)
            return True

    def mark_unknown(self, token: str = "", now: datetime | None = None) -> None:
        """Quarantine the image channel after an unresolved paid generation."""
        with self.lock:
            state = self._load()
            reservation = state["reservation"] or {}
            if token and reservation.get("token") != token:
                return
            moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            state["unknown_since"] = moment.isoformat()
            state["reservation"] = {}
            self._save(state)

    def release(self, token: str) -> None:
        """Drop a claim that definitely did not produce (or publish) an image."""
        with self.lock:
            state = self._load()
            reservation = state["reservation"] or {}
            if reservation.get("token") != token:
                return
            state["reservation"] = {}
            self._save(state)

    def mark_success(self, now: datetime | None = None) -> None:
        """Start the 48-hour cooldown after an image was actually published."""
        with self.lock:
            state = self._load()
            moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            state["last_success_at"] = moment.isoformat()
            state["unknown_since"] = ""
            state["reservation"] = {}
            self._save(state)

    def status(self) -> dict:
        """Read-only snapshot for logs and diagnostics."""
        with self.lock:
            state = self._load()
            return {
                "last_success_at": state["last_success_at"],
                "unknown_since": state["unknown_since"],
                "reservation": dict(state["reservation"] or {}),
            }
