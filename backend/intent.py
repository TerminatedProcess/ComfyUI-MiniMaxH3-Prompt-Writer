"""Intent resolution: what the user meant, before any prose exists.

No host, provider, or graph dependencies. The model call lives in the pipeline
layer; everything here is pure so it can be tested without a GPU.

Mode selection is the jargon the target user should never meet. "T2VA" and
"FL2VA" mean nothing to someone who just wants a video, so the mode is inferred
from what they dropped in and shown back in plain English. An explicit choice
always wins -- inference is a default, not a policy.
"""
from __future__ import annotations

from typing import Any

from .guides import MODE_GUIDES
from .scene_bible import (
    FIELDS,
    ORIGIN_ASSET,
    ORIGIN_INVENTED,
    ORIGIN_USER,
    SceneBibleError,
    new_bible,
    set_field,
)

VIDEO_MODES = tuple(m for m in MODE_GUIDES if m != "Music3")

# H3 reference ceilings, from the official guide and the native node's slots.
MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIO = 3

# Plain-English rendering of each mode, for a user who has never heard of I2VA.
MODE_SUMMARY = {
    "T2VA": "from your description alone",
    "I2VA": "starting from your image",
    "FL2VA": "from your first image to your last",
    "L2VA": "ending on your image",
    "Reference": "using your images as references",
}


class IntentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def infer_mode(
    image_count: int,
    video_count: int = 0,
    audio_count: int = 0,
    *,
    explicit: str | None = None,
) -> str:
    """Pick a generation mode from what the user supplied.

    0 images -> T2VA, 1 -> I2VA, 2 -> FL2VA, 3+ -> Reference. Any video or audio
    reference forces Reference, which is the only mode whose guide describes how
    to bind them.

    L2VA is never inferred: a single image is indistinguishable from an I2VA
    first frame, and guessing "ends on this image" when the user meant "starts
    on this image" silently inverts their intent. It stays opt-in.
    """
    if explicit is not None:
        if explicit not in VIDEO_MODES:
            raise IntentError("UNKNOWN_MODE", f"Unknown generation mode: {explicit}")
        return explicit
    for name, count, ceiling in (
        ("images", image_count, MAX_IMAGES),
        ("videos", video_count, MAX_VIDEOS),
        ("audio clips", audio_count, MAX_AUDIO),
    ):
        if count < 0:
            raise IntentError("NEGATIVE_COUNT", f"Cannot have {count} {name}.")
        if count > ceiling:
            raise IntentError(
                "TOO_MANY_REFERENCES",
                f"MiniMax H3 accepts at most {ceiling} {name}; {count} supplied.",
            )
    if video_count or audio_count:
        return "Reference"
    if image_count == 0:
        return "T2VA"
    if image_count == 1:
        return "I2VA"
    if image_count == 2:
        return "FL2VA"
    return "Reference"


def describe_mode(mode: str) -> str:
    if mode not in MODE_SUMMARY:
        raise IntentError("UNKNOWN_MODE", f"Unknown generation mode: {mode}")
    return MODE_SUMMARY[mode]


def shot_budget(duration_seconds: float) -> tuple[int, int]:
    """Shots appropriate to a duration, per the official guide's budget table.

    Returned as (minimum, maximum) so the expander can be told how much story to
    invent. Over-cutting a short clip is the most common way an H3 prompt
    produces a slideshow.
    """
    if duration_seconds <= 0:
        raise IntentError("INVALID_DURATION", "Duration must be positive.")
    if duration_seconds < 4 or duration_seconds > 15:
        raise IntentError(
            "DURATION_OUT_OF_RANGE",
            "MiniMax H3 supports 4 to 15 second clips.",
        )
    if duration_seconds <= 6:
        return (1, 2)
    if duration_seconds <= 10:
        return (2, 3)
    return (3, 5)


def _field_value(raw: Any, key: str) -> str:
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str) or not raw.strip():
        raise IntentError("MISSING_FIELD", f"The model returned no value for '{key}'.")
    return raw.strip()


def bible_from_resolution(
    resolved: dict[str, Any],
    *,
    asset_fields: tuple[str, ...] = (),
    brief: str = "",
) -> dict[str, Any]:
    """Build a scene bible from the resolver's structured output.

    Every field must be present. A field the resolver skips would be re-decided
    differently on the next turn, so an incomplete resolution is an error rather
    than something to paper over with defaults.

    `asset_fields` is passed in by the caller, which knows structurally which
    fields came from analysing an uploaded image. It is never inferred from the
    text: asking the model to classify its own inputs was measured at 0/6, and
    it erred toward marking the user's own words as invented, which would let
    them be silently overwritten.
    """
    if not isinstance(resolved, dict):
        raise IntentError("INVALID_RESOLUTION", "The resolver output must be an object.")
    unknown = [k for k in asset_fields if k not in FIELDS]
    if unknown:
        raise IntentError("UNKNOWN_FIELD", f"Unknown asset field(s): {sorted(unknown)}")

    lowered = brief.lower()
    bible = new_bible()
    for key in FIELDS:
        if key not in resolved:
            raise IntentError("MISSING_FIELD", f"The resolver omitted '{key}'.")
        value = _field_value(resolved[key], key)
        if key in asset_fields:
            origin = ORIGIN_ASSET
        else:
            # Cosmetic only -- it colours the UI so the user can see what was
            # invented. Nothing branches on it, so an approximate answer via the
            # model's own quote is fine; a bad quote just downgrades to invented.
            quote = resolved[key].get("quote") if isinstance(resolved[key], dict) else None
            cited = isinstance(quote, str) and quote.strip().lower() in lowered
            origin = ORIGIN_USER if cited and quote.strip() else ORIGIN_INVENTED
        try:
            bible = set_field(bible, key, value, origin)
        except SceneBibleError as error:
            raise IntentError(error.code, error.message) from error
    return bible
