"""MiniMax H3 video and MiniMax Music 3 -- the targets this writer started as.

Nothing about their behaviour changes here: the audit, the reference policy and
the narrow-repair contract are the same code paths as before, reached through the
registry instead of through `if mode == ...`. The one addition is flag-driven
system prompts, and with both flags off the wrappers are byte-identical to the
constants they were before (see tests/test_targets.py).
"""
from __future__ import annotations

from typing import Any

from ..prompt_audit import audit_prompt, camera_structure_requested
from ..prompt_repair import (
    audit_failures,
    dialogue_lines,
    explicit_constraint_violations,
    multimodal_repair_messages,
    narrow_repair_messages,
    unexpected_audio_task,
)
from ..references import reference_policy, reference_tags
from ..system_prompts import (
    MUSIC3_LYRICS_SYSTEM_WRAPPER,
    MUSIC3_SYSTEM_WRAPPER,
    REFERENCE_SYSTEM_WRAPPER,
    SYSTEM_WRAPPER,
)
from .base import Mode, Target, compose, shared_checks

# Sentences the flags edit. Each must exist verbatim in its wrapper; `compose`
# raises if one drifts, so a reworded wrapper cannot silently disable a flag.
STANDARD_NO_INVENTION = (
    "Treat the user's brief and supplied references as the factual boundary: do not invent "
    "unsupported subject actions, expressions, events, transitions, visible text, props, "
    "locations, or other reference-derived details. "
)
REFERENCE_NO_INVENTION = (
    "Preserve the source motion order, but do not invent unsupported subject actions, expressions, "
    "events, transitions, visible text, props, locations, or other reference-derived details. "
)
MUSIC3_NO_INVENTION = (
    "Do not invent a title, track ID, exact BPM, key, vocal gender, language, duration, or "
    "technical detail when a broader description is sufficient. "
)

STORY_VIDEO = (
    "Where the brief leaves the scene unspecified, design it: choose the supporting action, staging, "
    "wardrobe detail, props, light direction, weather and atmosphere that make the shot feel authored, "
    "and commit to them specifically rather than hedging. Never contradict the brief, an established "
    "fact, or anything visible in a supplied reference, and never invent dialogue, on-screen text, a "
    "named location, a period, or music that was not requested."
)
STORY_REFERENCE = (
    "Where the brief leaves the target scene unspecified, design it richly -- supporting action, "
    "staging, wardrobe detail, atmosphere, light direction -- and commit to it specifically. Never "
    "present designed content as a reference-derived fact, never contradict a reference, and never "
    "invent dialogue, on-screen text or music that was not requested."
)
STORY_MUSIC = (
    "Where the brief leaves a musical choice open, make it: commit to specific instrumentation, "
    "arrangement moves, textures and production decisions instead of hedging, while never contradicting "
    "an explicit requirement or exclusion."
)
NSFW_PERMITTED = (
    "Adult, explicit or otherwise mature content is permitted when the user's brief calls for it: write "
    "it plainly and specifically, without euphemism, moralising or refusal. Never add sexual content the "
    "brief does not ask for."
)

MODE_PROFILE = {
    "T2VA": "standard",
    "I2VA": "standard",
    "FL2VA": "standard",
    "L2VA": "standard",
    "Reference": "reference",
}
REFERENCE_LIMITS = {"image": 9, "video": 3, "audio": 3, "total": 12}


def video_system_prompt(mode: str, *, nsfw: bool = True, story: bool = True, variant: str | None = None) -> str:
    base = REFERENCE_SYSTEM_WRAPPER if mode == "Reference" else SYSTEM_WRAPPER
    remove: tuple[str, ...] = ()
    add: list[str] = []
    if story:
        if mode == "Reference":
            remove = (REFERENCE_NO_INVENTION,)
            add.append("Preserve the source motion order.")
            add.append(STORY_REFERENCE)
        else:
            remove = (STANDARD_NO_INVENTION,)
            add.append(STORY_VIDEO)
    if nsfw:
        add.append(NSFW_PERMITTED)
    return compose(base, remove=remove, add=tuple(add))


def music_system_prompt(mode: str, *, nsfw: bool = True, story: bool = True, variant: str | None = None) -> str:
    if mode == "Music3Lyrics":
        return compose(MUSIC3_LYRICS_SYSTEM_WRAPPER, add=(NSFW_PERMITTED,) if nsfw else ())
    remove = (MUSIC3_NO_INVENTION,) if story else ()
    add: list[str] = []
    if story:
        add.append(STORY_MUSIC)
    if nsfw:
        add.append(NSFW_PERMITTED)
    return compose(MUSIC3_SYSTEM_WRAPPER, remove=remove, add=tuple(add))


def _intent_text(assembled: dict[str, Any]) -> str:
    source = assembled.get("input", {})
    return "\n".join(
        str(source.get(key, ""))
        for key in ("creative_brief", "current_prompt", "instruction")
        if source.get(key)
    )


def audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
    """The existing H3 audit, plus the reference-inventory checks for Reference."""
    source = assembled["input"]
    mode = source["mode"]
    duration_seconds = source.get("duration_seconds")
    intent_text = _intent_text(assembled)
    camera_structure_allowed = camera_structure_requested(intent_text)
    result = audit_prompt(
        prompt,
        mode,
        duration_seconds,
        camera_structure_allowed,
        bible=source.get("bible"),
    )
    policy = reference_policy(source)
    actual = reference_tags(prompt)
    result["camera_structure_allowed"] = camera_structure_allowed
    if mode == "Reference":
        result["missing_reference_tags"] = sorted(policy.required - actual)
        result["unexpected_reference_tags"] = sorted(actual - policy.allowed)
        result["required_reference_tags"] = sorted(policy.required)
        result["mutable_reference_tags"] = sorted(policy.mutable)
        result["allowed_reference_tags"] = sorted(policy.allowed)
        result["unexpected_audio_task"] = unexpected_audio_task(result.get("task_label"), actual)
        result["explicit_constraint_violations"] = explicit_constraint_violations(intent_text, prompt)
        result["repair_required"] = bool(
            result.get("repair_required")
            or result["missing_reference_tags"]
            or result["unexpected_reference_tags"]
            or result["unexpected_audio_task"]
            or result["explicit_constraint_violations"]
        )
    shared = shared_checks(assembled, prompt)
    repair_required = bool(result.get("repair_required") or shared.get("repair_required"))
    result.update(shared)
    result["repair_required"] = repair_required
    return result


def _repair_tags(assembled: dict[str, Any], prompt: str) -> tuple[set[str], set[str], set[str]]:
    policy = reference_policy(assembled["input"])
    initial = reference_tags(prompt)
    return policy.required, policy.allowed, (initial & policy.allowed) | policy.required


def repair_plan(
    assembled: dict[str, Any],
    messages: list[dict[str, Any]],
    prompt: str,
    audit_result: dict[str, Any],
) -> dict[str, Any]:
    """Pick the narrow text correction or the multimodal one, as before."""
    required, allowed, repair_reference_tags = _repair_tags(assembled, prompt)
    failures = audit_failures(audit_result)
    duration_seconds = assembled["input"].get("duration_seconds")
    has_visual = any(item.get("type") in {"image", "video"} for item in assembled.get("media_inputs", []))
    if audit_result.get("missing_reference_tags") and has_visual:
        return {
            "messages": multimodal_repair_messages(
                messages, prompt, failures, repair_reference_tags, duration_seconds, allowed,
            ),
            "method": "multimodal reference correction",
            "reason": ", ".join(failures) or "official format audit",
            "expected_tags": repair_reference_tags,
        }
    return {
        "messages": narrow_repair_messages(
            assembled, prompt, failures, repair_reference_tags, duration_seconds, allowed,
        ),
        "method": "narrow text correction",
        "reason": ", ".join(failures) or "official format audit",
        "expected_tags": repair_reference_tags,
    }


def accept_repair(
    assembled: dict[str, Any],
    prompt: str,
    repaired: str,
    plan: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    """Re-audit the correction. A repair is never accepted unchecked."""
    repaired_audit = audit(repaired, assembled)
    # Order matters and is load-bearing for the reported reason: a draft that
    # still fails its own audit says so, and only an otherwise-clean correction
    # is blamed for changing the inventory.
    if repaired_audit.get("repair_required") is True:
        return False, repaired_audit, "repaired draft still failed: " + ", ".join(audit_failures(repaired_audit))
    if reference_tags(repaired) != plan["expected_tags"]:
        return False, repaired_audit, "correction changed the reference inventory or user dialogue"
    if dialogue_lines(repaired) != dialogue_lines(prompt):
        return False, repaired_audit, "correction changed the reference inventory or user dialogue"
    return True, repaired_audit, None


def _video_modes() -> tuple[Mode, ...]:
    return (
        Mode("T2VA", "From a description", "from your description alone", ("base",), {}, "standard"),
        Mode("I2VA", "From your image", "starting from your image", ("base",), {"image": 1}, "standard"),
        Mode("FL2VA", "First and last frame", "from your first image to your last", ("base",), {"image": 2}, "standard"),
        Mode("L2VA", "Ending on your image", "ending on your image", ("base",), {"image": 1}, "standard"),
        # Reference is written from the reference guide plus a selected excerpt of
        # the shared base rules, which the assembler appends separately -- listing
        # "base" here would inject the whole base guide a second time.
        Mode("Reference", "Using references", "using your images as references", ("reference",), REFERENCE_LIMITS, "reference"),
    )


def infer_mode(image_count: int, video_count: int = 0, audio_count: int = 0) -> str:
    """0 images -> T2VA, 1 -> I2VA, 2 -> FL2VA, 3+ or any clip -> Reference.

    L2VA is never inferred: one image is indistinguishable from an I2VA first
    frame, and guessing "ends on this image" inverts the user's intent.
    """
    if video_count or audio_count:
        return "Reference"
    if image_count >= 3:
        return "Reference"
    return ("T2VA", "I2VA", "FL2VA")[image_count]


H3 = Target(
    id="h3",
    label="MiniMax H3",
    vendor="MiniMax",
    workspace="video",
    media_kind="video",
    modes=_video_modes(),
    fields=frozenset({"duration_seconds", "aspect_ratio", "nsfw", "story"}),
    output_shape="sections",
    brief_limit=8000,
    output_tokens="standard",
    system_prompt=video_system_prompt,
    audit=audit,
    repair_plan=repair_plan,
    accept_repair=accept_repair,
    infer_mode=infer_mode,
    supports_sequence=True,
)

MUSIC3 = Target(
    id="music3",
    label="MiniMax Music 3",
    vendor="MiniMax",
    workspace="music",
    media_kind="audio",
    modes=(
        Mode("Music3", "Structured caption", "as a structured music caption", (), {}, "music3"),
        Mode("Music3Lyrics", "Lyrics", "as song lyrics", (), {}, "music3_lyrics", generation=False),
    ),
    fields=frozenset({"lyrics", "nsfw", "story"}),
    output_shape="lyrics",
    brief_limit=2000,
    output_tokens="music",
    system_prompt=music_system_prompt,
    audit=audit,
    repair_plan=repair_plan,
    accept_repair=accept_repair,
)
