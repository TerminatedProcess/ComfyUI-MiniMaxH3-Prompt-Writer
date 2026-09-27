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

from .scene_bible import distinctive_stems, distinctive_tokens, name_check, proper_nouns
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
    # Pose is separate from action on purpose. "Taking a selfie at her desk"
    # carried both, and whichever half did not fit the field's word budget was
    # the half that got dropped -- usually the body, so "standing" quietly
    # became "sitting" between compiles. A field of its own cannot be omitted,
    # locks like any other fact, and can be corrected by name.
    "pose",
    "expression",
    "location",
    "setting_detail",
    "era",
    "time_of_day",
    "weather",
    # Where everything sits, relative to the subject AND the camera. "In front
    # of the desk" is ambiguous English -- in front from whose side? -- so it
    # renders differently run to run even when the words survive intact.
    "staging",
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
    "pose": "Pose",
    "expression": "Expression",
    "location": "Location",
    "setting_detail": "Setting detail",
    "era": "Era",
    "time_of_day": "Time of day",
    "weather": "Weather",
    "staging": "Staging",
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
    ("Subject", ("subject", "appearance", "wardrobe", "action", "pose", "expression")),
    ("Scene", ("location", "setting_detail", "era", "time_of_day", "weather")),
    # Staging leads the Look group because it is camera-relative: it reads as
    # the first thing you decide about the shot, and it sits next to Camera.
    ("Look", ("staging", "camera", "lighting", "style", "mood", "visible_text")),
    ("Sound", ("dialogue", "soundscape", "music")),
    ("Constraints", ("exclusions",)),
)

# ----------------------------------------------------------------- people
#
# A document used to describe exactly one person, so two people shared one
# `wardrobe` string and nothing said whose the red dress was. The compile could
# swap them and no check could see it, because there was nothing to bind an
# attribute TO.
#
# So the subject fields repeat per person, and a person is a key suffix rather
# than a nested structure: `wardrobe` is A's, `wardrobe#2` is B's. Everything
# downstream -- locks, the audit, goals, conversation patches, the UI rows --
# addresses fields by name, and keeps working untouched.
PERSON_FIELDS = ("subject", "appearance", "wardrobe", "action", "pose", "expression")
SCENE_FIELDS = tuple(key for key in FIELDS if key not in PERSON_FIELDS)
PERSON_SEPARATOR = "#"
# Past this the prompt is a crowd scene, which no target renders per-person
# anyway, and the document becomes unreadable.
MAX_PEOPLE = 6
PERSON_LETTERS = "ABCDEF"


def person_letter(index: int) -> str:
    return PERSON_LETTERS[index - 1] if 1 <= index <= len(PERSON_LETTERS) else str(index)


def person_key(field: str, index: int = 1) -> str:
    """The document key for one person's field. Person 1 keeps the bare name."""
    return field if index == 1 else f"{field}{PERSON_SEPARATOR}{index}"


def split_key(key: str) -> tuple[str, int] | None:
    """(field, person index) or None when the key is not a document field."""
    if key in FIELDS:
        return key, 1
    field, separator, suffix = key.partition(PERSON_SEPARATOR)
    if not separator or field not in PERSON_FIELDS or not suffix.isdigit():
        return None
    index = int(suffix)
    if not 2 <= index <= MAX_PEOPLE:
        return None
    return field, index


def is_field(key: Any) -> bool:
    return isinstance(key, str) and split_key(key) is not None


def sort_key(key: str) -> tuple[int, int, int]:
    """People first, in order, then the scene. It reads as a description."""
    field, index = split_key(key)
    if field in PERSON_FIELDS:
        return (0, index, PERSON_FIELDS.index(field))
    return (1, 0, SCENE_FIELDS.index(field))


def people(doc: dict[str, Any]) -> tuple[int, ...]:
    """Which people this document describes. Always at least person 1."""
    found = {1}
    for key in (doc or {}).get("fields", {}):
        parsed = split_key(key)
        if parsed and parsed[0] in PERSON_FIELDS:
            found.add(parsed[1])
    return tuple(sorted(found))


def doc_fields(doc: dict[str, Any]) -> tuple[str, ...]:
    """Every addressable key for this document, in reading order."""
    keys = {person_key(field, index) for index in people(doc) for field in PERSON_FIELDS}
    keys.update(SCENE_FIELDS)
    return tuple(sorted(keys, key=sort_key))


def label_for(key: str) -> str:
    """"Wardrobe", or "Wardrobe (B)" -- an audit message must say whose."""
    parsed = split_key(key)
    if not parsed:
        return key
    field, index = parsed
    label = LABELS[field]
    return label if index == 1 else f"{label} ({person_letter(index)})"


def doc_labels(doc: dict[str, Any]) -> dict[str, str]:
    return {key: label_for(key) for key in doc_fields(doc)}


def doc_groups(doc: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The render/UI grouping: one block per person, then the scene groups."""
    indices = people(doc)
    blocks: list[tuple[str, tuple[str, ...]]] = []
    for index in indices:
        title = "Subject" if len(indices) == 1 else f"Subject {person_letter(index)}"
        blocks.append((title, tuple(person_key(field, index) for field in PERSON_FIELDS)))
    for title, keys in GROUPS:
        rest = tuple(key for key in keys if key not in PERSON_FIELDS)
        if rest:
            blocks.append((title, rest))
    return tuple(blocks)


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

# ---------------------------------------------------------------- relations
#
# Fields say what each thing IS; relations say how things sit against each
# other, which is the half a flat document could never hold. "In front of the
# desk" never said from whose side; "A occludes the desk" is true or false and
# you can check it by looking, because occlusion is the one spatial relation a
# single viewpoint defines exactly.
#
# The vocabulary is CLOSED on purpose. An open one is where a model invents
# relations that sound plausible, and every term here is answerable as a yes/no
# about the picture -- which is what makes an edge verifiable instead of merely
# asserted.
RELATIONS = {
    "on": "resting on or supported by",
    "in": "inside or enclosed by",
    "holding": "grasping or carrying",
    "wearing": "worn by",
    "occludes": "hides part of, from where the camera is",
    "behind": "further from the camera, without overlapping",
    "left_of": "to the left in frame",
    "right_of": "to the right in frame",
}
# Beyond this the document is describing furniture, not a shot.
MAX_EDGES = 12
MAX_ENDPOINT_CHARS = 60


def endpoint_person(endpoint: str) -> int | None:
    """The person an endpoint names, if it names one. "A" is person 1."""
    value = (endpoint or "").strip()
    if len(value) == 1 and value.upper() in PERSON_LETTERS[:MAX_PEOPLE]:
        return PERSON_LETTERS.index(value.upper()) + 1
    return None


def _clean_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise GenericError("INVALID_EDGE", "A relation endpoint must be text.")
    text = normalize_unicode_text(value).strip()
    if not text:
        raise GenericError("INVALID_EDGE", "A relation endpoint cannot be empty.")
    if len(text) > MAX_ENDPOINT_CHARS:
        raise GenericError("INVALID_EDGE", f"A relation endpoint cannot exceed {MAX_ENDPOINT_CHARS} characters.")
    person = endpoint_person(text)
    return PERSON_LETTERS[person - 1] if person else text


def validate_edge(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GenericError("INVALID_EDGE", "A relation must be an object.")
    relation = raw.get("rel")
    if relation not in RELATIONS:
        raise GenericError("INVALID_EDGE", f"Unknown relation: {relation!r}")
    edge = {
        "from": _clean_endpoint(raw.get("from")),
        "rel": relation,
        "to": _clean_endpoint(raw.get("to")),
    }
    if edge["from"] == edge["to"]:
        raise GenericError("INVALID_EDGE", "A relation needs two different things.")
    qualifier = raw.get("qualifier")
    if qualifier not in (None, ""):
        if not isinstance(qualifier, str):
            raise GenericError("INVALID_EDGE", "A relation qualifier must be text.")
        text = normalize_unicode_text(qualifier).strip()
        if len(text) > MAX_ENDPOINT_CHARS:
            raise GenericError("INVALID_EDGE", "A relation qualifier is too long.")
        if text:
            edge["qualifier"] = text
    origin = raw.get("origin", ORIGIN_INVENTED)
    if origin not in ORIGINS or origin == ORIGIN_UNSPECIFIED:
        raise GenericError("INVALID_EDGE", f"Unknown origin for a relation: {origin!r}")
    edge["origin"] = origin
    return edge


def edges(doc: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(doc.get("edges") or ())


def edge_text(edge: dict[str, Any]) -> str:
    """One relation, as a person would say it."""
    relation = edge["rel"].replace("_", " ")
    qualifier = f" ({edge['qualifier']})" if edge.get("qualifier") else ""
    return f"{edge['from']} {relation} {edge['to']}{qualifier}"


def set_edges(doc: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Replace the relation set. Duplicates collapse; order is preserved."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise GenericError("INVALID_EDGE", "Relations must be a list.")
    if len(raw) > MAX_EDGES:
        raise GenericError("INVALID_EDGE", f"A document cannot hold more than {MAX_EDGES} relations.")
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in raw:
        edge = validate_edge(item)
        seen[(edge["from"], edge["rel"], edge["to"])] = edge
    return validate({**doc, "schema": SCHEMA, "edges": list(seen.values()), "updated_at": time.time()})


def locked_edges(doc: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(edge for edge in edges(doc) if edge.get("origin") in LOCKED_ORIGINS)


def render_relations(doc: dict[str, Any]) -> str:
    return "; ".join(edge_text(edge) for edge in edges(doc))



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
        if not is_field(key):
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
    raw_edges = doc.get("edges")
    if raw_edges is not None:
        if not isinstance(raw_edges, list):
            raise GenericError("INVALID_DOC", "The relations must be a list.")
        if len(raw_edges) > MAX_EDGES:
            raise GenericError("INVALID_EDGE", f"A document cannot hold more than {MAX_EDGES} relations.")
        for item in raw_edges:
            validate_edge(item)
    return doc


def record(doc: dict[str, Any], key: str) -> dict[str, Any]:
    """The field as stored, or the empty unspecified record."""
    if not is_field(key):
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
    return {key: record(doc, key) for key in doc_fields(doc)}


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
    if not is_field(key):
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
    unknown = [key for key in patch if not is_field(key)]
    if unknown:
        raise GenericError("UNKNOWN_FIELD", f"Unknown generic field(s): {sorted(unknown)}")

    result = doc
    changed: list[str] = []
    protected: list[str] = []
    # Deterministic, regardless of patch ordering, and inclusive of a person
    # the patch is introducing for the first time.
    for key in sorted({*doc_fields(doc), *patch}, key=sort_key):
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
    return tuple(key for key in doc_fields(doc) if record(doc, key)["origin"] == ORIGIN_UNSPECIFIED)


def locked_fields(doc: dict[str, Any]) -> tuple[str, ...]:
    return tuple(key for key in doc_fields(doc) if record(doc, key)["origin"] in LOCKED_ORIGINS)


def is_empty(doc: dict[str, Any]) -> bool:
    return not edges(doc) and not any(record(doc, key)["value"] for key in doc_fields(doc))


def render(doc: dict[str, Any]) -> str:
    """The generic prompt as the user reads it, and as a compile step receives it."""
    blocks: list[str] = []
    for title, keys in doc_groups(doc):
        lines = [
            f"{LABELS[split_key(key)[0]]}: {record(doc, key)['value']}"
            for key in keys
            if record(doc, key)["value"]
        ]
        if lines:
            blocks.append(f"{title}\n" + "\n".join(lines))
    relations = render_relations(doc)
    if relations:
        blocks.append(f"Relations\n{relations}")
    return "\n\n".join(blocks)


def render_constraints(doc: dict[str, Any]) -> str:
    """The established-facts block: every locked fact, restated as a constraint.

    The repetition IS the mechanism -- restating each fact on every compile is
    what stops a regeneration from drifting the subject.
    """
    lines = []
    for key in doc_fields(doc):
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
        lines.append(f"- {label_for(key)}: {entry['value']}{marker}")
    return "\n".join(lines)


def lock_violations(doc: dict[str, Any], prose: str) -> tuple[str, ...]:
    """Locked fields whose distinctive words are missing from a compiled prompt.

    Token-based on purpose: cheap enough to run after every compile, and drift
    shows up as vocabulary simply going missing. Names are all-or-nothing --
    "Bob, a man in his late 30s" rendered as "an adult male in his late 30s"
    keeps most tokens and loses the only handle the user steers with.

    The description is compared by stem, because the compile is expected to
    reword and a tense change is not a dropped fact: "Taking a selfie with her
    arm extended toward the camera, tilting her head slightly" restated as "she
    extends her right arm toward the camera to take a selfie" scored 0.58
    against the 0.6 bar on verb endings alone. Names are still matched exactly.
    """
    prose_tokens = distinctive_tokens(prose or "")
    prose_stems = distinctive_stems(prose or "")
    missing: list[str] = []
    for key in locked_fields(doc):
        value = record(doc, key)["value"]
        wanted = distinctive_stems(value)
        if not wanted:
            continue
        names, dropped_names = name_check(value, prose or "")
        if dropped_names:
            missing.append(key)
            continue
        threshold = 0.35 if names else 0.6
        if len(wanted & prose_stems) / len(wanted) < threshold:
            missing.append(key)
    return tuple(missing)


def lock_expected(doc: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    """The exact values a repair turn must restore, quoted verbatim."""
    return {key: record(doc, key)["value"] for key in keys}


