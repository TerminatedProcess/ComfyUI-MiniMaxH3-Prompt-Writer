"""Durable studio state: everything persists until the user presses Reset.

The generic prompt, the goal ledger, the conversation, the left-rail inputs and
the last compiled prompt per target all live here, on disk, under the extension's
own `data/sessions/`. Not the temp directory: that is wiped on every server start,
and "my conversation vanished because ComfyUI restarted" is not persistence.

Writes are atomic (temp file + os.replace). This code was written the morning
after a power cut ate a desktop mid-session; a half-written JSON file that loses
an afternoon of work is exactly the failure this avoids.

One thing deliberately does NOT survive a server restart: the uploaded media
itself, which still lives in the media store's temp cache. The document keeps a
snapshot of what was attached (filename, reference label, type) so the studio can
say "these images are no longer loaded -- re-add them to re-read the picture"
instead of silently failing a re-observation.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from . import generic, goals as goal_ledger

SCHEMA = "session/1"
STATE_ROOT = Path(__file__).resolve().parent.parent / "data" / "sessions"
MAX_CONVERSATION_TURNS = 200
MAX_MESSAGE_CHARS = 4000
MAX_GOAL_TEXT = 400


class SessionStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_session_id(session_id: str) -> str:
    """Reject anything that is not a UUID, so a session id cannot walk the path."""
    try:
        return str(UUID(str(session_id)))
    except (ValueError, AttributeError, TypeError) as error:
        raise SessionStoreError("INVALID_SESSION", "The session ID is invalid.") from error


def _path(session_id: str) -> Path:
    return STATE_ROOT / f"{_safe_session_id(session_id)}.json"


def new_state(session_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "session_id": _safe_session_id(session_id),
        "updated_at": None,
        "inputs": {
            "brief": "",
            "duration_seconds": 10,
            "aspect_ratio": "16:9",
            "nsfw": True,
            "story": True,
            "no_audio": True,
        },
        "generic": generic.new_doc(),
        "goals": [],
        "conversation": [],
        "outputs": {},
        "target": {"mode": None, "variant": None},
        "media_snapshot": [],
    }


def _normalized(raw: Any, session_id: str) -> dict[str, Any]:
    state = new_state(session_id)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return state
    inputs = raw.get("inputs")
    if isinstance(inputs, dict):
        merged = dict(state["inputs"])
        for key in merged:
            if key in inputs:
                merged[key] = inputs[key]
        merged["brief"] = str(merged.get("brief") or "")[:8000]
        merged["nsfw"] = bool(merged.get("nsfw", True))
        merged["story"] = bool(merged.get("story", True))
        merged["no_audio"] = bool(merged.get("no_audio", True))
        state["inputs"] = merged
    try:
        state["generic"] = generic.validate(raw.get("generic") or generic.new_doc())
    except generic.GenericError:
        # A corrupt document must not make the session unopenable. Losing the
        # document is bad; losing the conversation and goals as well is worse.
        state["generic"] = generic.new_doc()
    try:
        state["goals"] = goal_ledger.validate(raw.get("goals"))
    except goal_ledger.GoalError:
        state["goals"] = []
    conversation = raw.get("conversation")
    if isinstance(conversation, list):
        state["conversation"] = [
            {
                "role": "assistant" if turn.get("role") == "assistant" else "user",
                "text": str(turn.get("text") or "")[:MAX_MESSAGE_CHARS],
                "changed": [key for key in (turn.get("changed") or []) if key in generic.FIELDS],
                "protected": [key for key in (turn.get("protected") or []) if key in generic.FIELDS],
                "goals_added": [str(item)[:MAX_GOAL_TEXT] for item in (turn.get("goals_added") or [])],
                "at": turn.get("at"),
            }
            for turn in conversation
            if isinstance(turn, dict)
        ][-MAX_CONVERSATION_TURNS:]
    outputs = raw.get("outputs")
    if isinstance(outputs, dict):
        state["outputs"] = {
            str(mode): value for mode, value in outputs.items() if isinstance(value, dict)
        }
    target = raw.get("target")
    if isinstance(target, dict):
        state["target"] = {"mode": target.get("mode"), "variant": target.get("variant")}
    snapshot = raw.get("media_snapshot")
    if isinstance(snapshot, list):
        state["media_snapshot"] = [item for item in snapshot if isinstance(item, dict)]
    state["updated_at"] = raw.get("updated_at")
    return state


def load(session_id: str) -> dict[str, Any]:
    path = _path(session_id)
    if not path.exists():
        return new_state(session_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return new_state(session_id)
    return _normalized(raw, session_id)


def is_empty(state: dict[str, Any]) -> bool:
    """Whether this session holds anything worth a file on disk."""
    from . import generic as generic_doc

    if not generic_doc.is_empty(state.get("generic") or {}):
        return False
    if state.get("goals") or state.get("conversation") or state.get("outputs"):
        return False
    inputs = state.get("inputs") or {}
    defaults = new_state(state.get("session_id") or str(UUID(int=0)))["inputs"]
    return all(inputs.get(key) == value for key, value in defaults.items())


def save(state: dict[str, Any]) -> dict[str, Any]:
    session_id = _safe_session_id(state.get("session_id"))
    state["session_id"] = session_id
    state["schema"] = SCHEMA
    state["updated_at"] = time.time()
    path = _path(session_id)
    if is_empty(state):
        # Every page load mints a session id. Writing a file for one that holds
        # nothing would litter the state directory with a file per visit, and
        # nothing here is ever pruned -- by design, since the user's work is
        # supposed to survive until they press Reset.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return state
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return state


def reset(session_id: str) -> dict[str, Any]:
    """Wipe the whole session. This is the only thing that clears it."""
    path = _path(session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return new_state(session_id)


def merge_after_await(session_id: str, state: dict[str, Any], *owned: str) -> dict[str, Any]:
    """Re-read the session and keep only `owned` keys from the in-flight copy.

    A model call takes seconds to minutes, and the studio keeps writing during it:
    the brief debounce, the duration slider, an inline field edit. Saving the
    snapshot this handler loaded before the call would silently undo all of them
    -- observed as a brief reverting the moment a conversation turn landed.
    Whatever the handler owns wins; everything else is re-read.
    """
    current = load(session_id)
    for key in owned:
        current[key] = state.get(key, current.get(key))
    return current


def record_turn(
    state: dict[str, Any],
    role: str,
    text: str,
    *,
    changed: tuple[str, ...] = (),
    protected: tuple[str, ...] = (),
    goals_added: tuple[str, ...] = (),
) -> dict[str, Any]:
    turn = {
        "role": "assistant" if role == "assistant" else "user",
        "text": str(text or "")[:MAX_MESSAGE_CHARS],
        "changed": list(changed),
        "protected": list(protected),
        "goals_added": list(goals_added),
        "at": time.time(),
    }
    state["conversation"] = (state.get("conversation") or [])[-(MAX_CONVERSATION_TURNS - 1):] + [turn]
    return state


def record_output(
    state: dict[str, Any],
    mode: str,
    *,
    prompt: str,
    negative_prompt: str = "",
    variant: str | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep one compiled prompt per target, so switching the picker shows work.

    Blanking the output on every target switch would make the right-hand pane
    feel like it lost the prompt you just made.
    """
    outputs = dict(state.get("outputs") or {})
    outputs[mode] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt or "",
        "variant": variant,
        "audit": audit or {},
        "at": time.time(),
        # The document the prompt was compiled from, so the studio can mark an
        # output stale once the conversation has moved the generic prompt on.
        "generic_updated_at": (state.get("generic") or {}).get("updated_at"),
    }
    state["outputs"] = outputs
    return state


def media_snapshot(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": asset.get("id"),
            "filename": asset.get("filename"),
            "reference": asset.get("reference"),
            "type": asset.get("type"),
        }
        for asset in (manifest or {}).get("assets", [])
    ]


def public(state: dict[str, Any], *, attached: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The state the studio renders, plus what it needs to be honest about media.

    `media_missing` is the snapshot of assets the document was built from that
    are no longer loaded -- after a server restart, re-observing the picture is
    impossible until they are re-added, and the UI says so rather than quietly
    doing nothing.
    """
    loaded_ids = {item.get("id") for item in (attached or [])}
    snapshot = state.get("media_snapshot") or []
    return {
        **state,
        "fields": generic.records(state.get("generic") or generic.new_doc()),
        "field_order": list(generic.FIELDS),
        "labels": dict(generic.LABELS),
        "groups": [{"title": title, "fields": list(keys)} for title, keys in generic.GROUPS],
        "unspecified": list(generic.unspecified_fields(state.get("generic") or generic.new_doc())),
        "media_missing": [item for item in snapshot if item.get("id") not in loaded_ids],
    }
