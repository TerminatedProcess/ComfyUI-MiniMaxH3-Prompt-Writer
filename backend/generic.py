"""The generic prompt: one model-agnostic scene document, compiled per target.

This is the writer's centre of gravity. The user builds ONE document on the left
of the studio -- by brief, by conversation, or by editing a row -- and every
model prompt on the right is compiled from it. Switching from H3 to Krea 2 must
not lose a fact, so no fact may live in the compiled prose; it lives here.

Why structured rather than prose: measured, not assumed. Editing generated prose
makes the model anchor on that text and make surface substitutions (told to
restyle a room to the 1970s it renamed the era and kept the VHS tapes, across
five seeds); regenerating from scratch fixed thoroughness but silently replaced
the subject. Holding structured fields and re-rendering gets both.
See docs/PLAN_IMAGINATION_AND_ASSETS.md.

Origins are the other half. A field records where its value came from, which is
what lets a correction be applied to the right layer:

    unspecified  nothing has fixed it -- the compile step may invent it
    invented     story builder filled it -- an observation may replace it
    asset        read off a reference image -- locked, re-derived on re-observe
    user         the user said it -- locked, never re-observed away
    override     the user said it AND it contradicts what the image showed --
                 locked, sticky, and NEVER reverted by a later re-observation

Without `override` the two corrections "her skirt is red (you misread the image)"
and "make her skirt red (I know the image is blue)" are indistinguishable, and a
later re-observe turn silently undoes the second one.
"""
from __future__ import annotations

import time
from typing import Any

from .scene_bible import distinctive_tokens, proper_nouns
from .text_normalization import normalize_unicode_text


SCHEMA = "generic/1"

# Ordered. This is both the render order and the order facts are presented to a
# compile step, so it reads as a description: who, how they look, what they do,
# where, when, how it is shot, how it sounds, what must not appear.
FIELDS = (
    "subject",
    "appearance",
    "wardrobe",
    "action",
    "expression",
    "location",
    "setting_detail",
    "era",
    "time_of_day",
    "weather",
    "camera",
    "lighting",
    "style",
    "mood",
    "visible_text",
    "dialogue",
    "soundscape",
    "music",
    "exclusions",
)
LABELS = {
    "subject": "Subject",
    "appearance": "Appearance",
    "wardrobe": "Wardrobe",
    "action": "Action",
    "expression": "Expression",
    "location": "Location",
    "setting_detail": "Setting detail",
    "era": "Era",
    "time_of_day": "Time of day",
    "weather": "Weather",
    "camera": "Camera",
    "lighting": "Lighting",
    "style": "Style",
    "mood": "Mood",
    "visible_text": "Visible text",
    "dialogue": "Dialogue",
    "soundscape": "Soundscape",
    "music": "Music",
    "exclusions": "Must not appear",
}
GROUPS = (
    ("Subject", ("subject", "appearance", "wardrobe", "action", "expression")),
    ("Scene", ("location", "setting_detail", "era", "time_of_day", "weather")),
    ("Look", ("camera", "lighting", "style", "mood", "visible_text")),
    ("Sound", ("dialogue", "soundscape", "music")),
    ("Constraints", ("exclusions",)),
)

ORIGIN_UNSPECIFIED = "unspecified"
ORIGIN_INVENTED = "invented"
ORIGIN_ASSET = "asset"
ORIGIN_USER = "user"
ORIGIN_OVERRIDE = "override"
ORIGINS = (ORIGIN_UNSPECIFIED, ORIGIN_INVENTED, ORIGIN_ASSET, ORIGIN_USER, ORIGIN_OVERRIDE)
# Facts the compile step may not drop, and the audit checks for afterwards.
LOCKED_ORIGINS = (ORIGIN_ASSET, ORIGIN_USER, ORIGIN_OVERRIDE)
# A re-observation may correct these; the rest are the user's and are left alone.
OBSERVABLE_ORIGINS = (ORIGIN_UNSPECIFIED, ORIGIN_INVENTED, ORIGIN_ASSET)

SOURCE_USER = "user"
SOURCE_OBSERVE = "observe"
SOURCE_INVENT = "invent"
SOURCES = (SOURCE_USER, SOURCE_OBSERVE, SOURCE_INVENT)

MAX_FIELD_CHARS = 600


class GenericError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_doc() -> dict[str, Any]:
    return {"schema": SCHEMA, "fields": {}, "updated_at": None}


def _clean(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise GenericError("INVALID_FIELD", f"{LABELS.get(key, key)} must be text.")
    text = normalize_unicode_text(value).strip()
    if len(text) > MAX_FIELD_CHARS:
        raise GenericError("FIELD_TOO_LONG", f"{LABELS.get(key, key)} cannot exceed {MAX_FIELD_CHARS} characters.")
    return text


def validate(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise GenericError("INVALID_DOC", "The generic prompt must be an object.")
    if doc.get("schema") not in (None, SCHEMA):
        raise GenericError("INVALID_DOC", "Unsupported generic prompt schema.")
    fields = doc.get("fields")
    if not isinstance(fields, dict):
        raise GenericError("INVALID_DOC", "The generic prompt is malformed.")
    for key, record in fields.items():
        if key not in FIELDS:
            raise GenericError("UNKNOWN_FIELD", f"Unknown generic field: {key}")
        if not isinstance(record, dict):
            raise GenericError("INVALID_FIELD", f"Field '{key}' is malformed.")
        origin = record.get("origin")
        if origin not in ORIGINS:
            raise GenericError("INVALID_ORIGIN", f"Unknown origin '{origin}' for field '{key}'.")
        value = _clean(record.get("value", ""), key)
        if not value and origin != ORIGIN_UNSPECIFIED:
            raise GenericError("EMPTY_FIELD", f"Field '{key}' has an origin but no value.")
        observed = record.get("observed")
        if observed is not None:
            _clean(observed, key)
    return doc


def record(doc: dict[str, Any], key: str) -> dict[str, Any]:
    """The field as stored, or the empty unspecified record."""
    if key not in FIELDS:
        raise GenericError("UNKNOWN_FIELD", f"Unknown generic field: {key}")
    stored = doc.get("fields", {}).get(key)
    if not isinstance(stored, dict):
        return {"value": "", "origin": ORIGIN_UNSPECIFIED, "observed": None}
    return {
        "value": stored.get("value", ""),
        "origin": stored.get("origin", ORIGIN_UNSPECIFIED),
        "observed": stored.get("observed"),
    }


def records(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: record(doc, key) for key in FIELDS}


def set_field(
    doc: dict[str, Any],
    key: str,
    value: str,
    origin: str,
    *,
    observed: str | None = None,
) -> dict[str, Any]:
    if origin not in ORIGINS:
        raise GenericError("INVALID_ORIGIN", f"Unknown origin: {origin}")
    text = _clean(value, key)
    if not text and origin != ORIGIN_UNSPECIFIED:
        raise GenericError("EMPTY_FIELD", f"{LABELS.get(key, key)} cannot be empty.")
    fields = dict(doc.get("fields", {}))
    if origin == ORIGIN_UNSPECIFIED and not text:
        fields.pop(key, None)
    else:
        entry: dict[str, Any] = {"value": text, "origin": origin}
        if observed is not None:
            entry["observed"] = _clean(observed, key)
        fields[key] = entry
    return validate({**doc, "schema": SCHEMA, "fields": fields, "updated_at": time.time()})


def clear_field(doc: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in FIELDS:
        raise GenericError("UNKNOWN_FIELD", f"Unknown generic field: {key}")
    fields = {name: value for name, value in doc.get("fields", {}).items() if name != key}
    return validate({**doc, "schema": SCHEMA, "fields": fields, "updated_at": time.time()})


def apply_patch(
    doc: dict[str, Any],
    patch: dict[str, Any],
    *,
    source: str,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Merge a field patch under the rules of where it came from.

    Returns (doc, changed, protected). `protected` names fields a re-observation
    declined to touch because the user owns them -- reported rather than
    swallowed, so "I corrected it and nothing happened" is visible in the log.
    """
    if source not in SOURCES:
        raise GenericError("INVALID_SOURCE", f"Unknown patch source: {source}")
    if not isinstance(patch, dict):
        raise GenericError("INVALID_PATCH", "The field patch must be an object.")
    unknown = [key for key in patch if key not in FIELDS]
    if unknown:
        raise GenericError("UNKNOWN_FIELD", f"Unknown generic field(s): {sorted(unknown)}")

    result = doc
    changed: list[str] = []
    protected: list[str] = []
    for key in FIELDS:  # deterministic, regardless of patch ordering
        if key not in patch:
            continue
        raw = patch[key]
        if isinstance(raw, dict):
            raw = raw.get("value")
        value = _clean(raw, key)
        if not value:
            continue
        current = record(doc, key)
        if source == SOURCE_INVENT:
            # Story builder fills gaps. It never argues with an existing fact.
            if current["origin"] != ORIGIN_UNSPECIFIED:
                continue
            result = set_field(result, key, value, ORIGIN_INVENTED)
            changed.append(key)
            continue
        if source == SOURCE_OBSERVE:
            if current["origin"] not in OBSERVABLE_ORIGINS:
                # The user's own value, or a deliberate deviation from the image.
                # Re-reading the reference must not walk either of them back.
                protected.append(key)
                continue
            if value == current["value"] and current["origin"] == ORIGIN_ASSET:
                continue
            result = set_field(result, key, value, ORIGIN_ASSET, observed=value)
            changed.append(key)
            continue
        # SOURCE_USER: the user's words outrank the image, and a contradiction of
        # something observed is recorded as a deliberate override so it sticks.
        observed = current["observed"] or (current["value"] if current["origin"] == ORIGIN_ASSET else None)
        if value == current["value"] and current["origin"] in (ORIGIN_USER, ORIGIN_OVERRIDE):
            continue
        origin = ORIGIN_OVERRIDE if observed and observed != value else ORIGIN_USER
        result = set_field(result, key, value, origin, observed=observed)
        changed.append(key)
    return result, tuple(changed), tuple(protected)




def unspecified_fields(doc: dict[str, Any]) -> tuple[str, ...]:
    return tuple(key for key in FIELDS if record(doc, key)["origin"] == ORIGIN_UNSPECIFIED)


def locked_fields(doc: dict[str, Any]) -> tuple[str, ...]:
    return tuple(key for key in FIELDS if record(doc, key)["origin"] in LOCKED_ORIGINS)


def is_empty(doc: dict[str, Any]) -> bool:
    return not any(record(doc, key)["value"] for key in FIELDS)


def render(doc: dict[str, Any]) -> str:
    """The generic prompt as the user reads it, and as a compile step receives it."""
    blocks: list[str] = []
    for title, keys in GROUPS:
        lines = [
            f"{LABELS[key]}: {record(doc, key)['value']}"
            for key in keys
            if record(doc, key)["value"]
        ]
        if lines:
            blocks.append(f"{title}\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def render_constraints(doc: dict[str, Any]) -> str:
    """The established-facts block: every locked fact, restated as a constraint.

    The repetition IS the mechanism -- restating each fact on every compile is
    what stops a regeneration from drifting the subject.
    """
    lines = []
    for key in FIELDS:
        entry = record(doc, key)
        if not entry["value"] or entry["origin"] == ORIGIN_UNSPECIFIED:
            continue
        marker = ""
        if entry["origin"] == ORIGIN_ASSET:
            marker = " [from the user's reference media - reproduce exactly]"
        elif entry["origin"] == ORIGIN_OVERRIDE:
            marker = (
                f" [the user's deliberate choice, replacing {entry['observed']!r} "
                "seen in the reference - use the user's value]"
            )
        elif entry["origin"] == ORIGIN_USER:
            marker = " [the user's own words - reproduce faithfully]"
        lines.append(f"- {LABELS[key]}: {entry['value']}{marker}")
    return "\n".join(lines)


def lock_violations(doc: dict[str, Any], prose: str) -> tuple[str, ...]:
    """Locked fields whose distinctive words are missing from a compiled prompt.

    Token-based on purpose: cheap enough to run after every compile, and drift
    shows up as vocabulary simply going missing. Names are all-or-nothing --
    "Bob, a man in his late 30s" rendered as "an adult male in his late 30s"
    keeps most tokens and loses the only handle the user steers with.
    """
    prose_tokens = distinctive_tokens(prose or "")
    missing: list[str] = []
    for key in locked_fields(doc):
        value = record(doc, key)["value"]
        wanted = distinctive_tokens(value)
        if not wanted:
            continue
        names = proper_nouns(value)
        if names - prose_tokens:
            missing.append(key)
            continue
        threshold = 0.35 if names else 0.6
        if len(wanted & prose_tokens) / len(wanted) < threshold:
            missing.append(key)
    return tuple(missing)


def lock_expected(doc: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    """The exact values a repair turn must restore, quoted verbatim."""
    return {key: record(doc, key)["value"] for key in keys}


