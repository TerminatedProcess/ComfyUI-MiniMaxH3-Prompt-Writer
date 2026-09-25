from __future__ import annotations

import re
from typing import Any

from . import generic as generic_prompt
from . import goals as goal_ledger
from .guides import MODE_GUIDES, guide_for_mode, load_guide, reference_base_excerpt
from .scene_bible import (
    SceneBibleError,
    locked_fields,
    render_constraints,
    validate as validate_bible,
)
from .media import STORE, parse_session_id
from .references import canonical_reference_tags
from .system_prompts import SystemPromptError, resolve_system_prompt
from .targets import TargetError, mode_spec, target_for_mode
from .text_normalization import normalize_unicode_text


ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}
CAPABILITY_BY_TYPE = {"image": "images", "video": "video_frames", "audio": "audio"}
MUSIC3_MODE = "Music3"


def _target(mode: str):
    try:
        return target_for_mode(mode)
    except TargetError as error:
        raise AssemblyError("INVALID_MODE", "The selected generation mode is not supported.") from error


def _flags(body: dict[str, Any], mode: str) -> dict[str, Any]:
    """Resolve Naughty, Story builder and the target's variant for this request.

    Both flags default ON -- that is the product decision, and it is applied
    here rather than in `system_prompt_for_mode` so the literal wrappers stay
    the auditable baseline the flags edit.
    """
    target = _target(mode)
    for key in ("nsfw", "story"):
        if key in body and not isinstance(body[key], bool):
            raise AssemblyError("INVALID_REQUEST", f"{key} must be a boolean.", {"field": key})
    nsfw = bool(body.get("nsfw", True)) if target.declares("nsfw") else False
    story = bool(body.get("story", True)) if target.declares("story") else False
    variant = None
    if target.variants:
        requested = body.get("variant") or target.default_variant
        if requested not in target.variants:
            raise AssemblyError(
                "INVALID_VARIANT",
                f"{target.label} has no variant {requested!r}.",
                {"variants": list(target.variants)},
            )
        variant = requested
    elif body.get("variant"):
        raise AssemblyError("INVALID_VARIANT", f"{target.label} has no variants.", {"field": "variant"})
    return {"nsfw": nsfw, "story": story, "variant": variant}


def _validated_generic(body: dict[str, Any]) -> dict[str, Any] | None:
    """The generic prompt document, when the request is compiling one.

    A malformed document is rejected rather than ignored: dropping it would
    silently disable every lock it carries, which is the drift locks exist to
    catch.
    """
    raw = body.get("generic")
    if raw is None:
        return None
    try:
        generic_prompt.validate(raw)
    except generic_prompt.GenericError as error:
        raise AssemblyError(error.code, error.message) from error
    return None if generic_prompt.is_empty(raw) else raw


def _validated_goals(body: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return goal_ledger.validate(body.get("goals"))
    except goal_ledger.GoalError as error:
        raise AssemblyError(error.code, error.message) from error


def _established_block(doc: dict[str, Any] | None, bible: dict[str, Any] | None) -> str:
    """Facts the compile step must reproduce, restated in full every time."""
    established = ""
    if doc is not None:
        established = generic_prompt.render_constraints(doc)
    elif bible is not None:
        established = render_constraints(bible)
    if not established:
        return ""
    return (
        "Established facts fixed by the user's reference media and their own words. Reproduce each one "
        "faithfully; never substitute a different subject, wardrobe or setting for them:\n"
        f"{established}\n\n"
    )


def _goal_block(goals: list[dict[str, Any]]) -> str:
    """Standing goals, injected into every compile -- not just the turn they were set.

    A goal that only applied to the turn that created it is not a goal; it is a
    one-off patch the user has to keep repeating for every model.
    """
    rendered = goal_ledger.render(goals)
    if not rendered:
        return ""
    return (
        "Standing goals from the user. Every one of them must hold in this prompt; they outrank your own "
        "judgement and any default:\n"
        f"{rendered}\n\n"
    )


class AssemblyError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _validated_bible(body: dict[str, Any]) -> dict[str, Any] | None:
    """Return the request's scene bible, or None when it carries no locks.

    A malformed bible is rejected rather than ignored: silently dropping it
    would disable asset locks without telling anyone, which is precisely the
    silent-drift failure locks exist to prevent.
    """
    raw = body.get("bible")
    if raw is None:
        return None
    try:
        validate_bible(raw)
    except SceneBibleError as error:
        raise AssemblyError(error.code, error.message) from error
    return raw if locked_fields(raw) else None


def _required_text(body: dict[str, Any], key: str, label: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AssemblyError("INVALID_REQUEST", f"{label} is required.", {"field": key})
    return normalize_unicode_text(value).strip()


def _media_line(asset: dict[str, Any]) -> str:
    detail = asset["type"]
    if asset.get("duration") is not None:
        detail += f", {asset['duration']:g}s"
    if asset["type"] == "video":
        times = ", ".join(f"{frame['timestamp']:g}s" for frame in asset.get("frames", []))
        if times:
            detail += f", sampled frames at {times}"
    elif asset["type"] == "audio":
        detail += ", not analyzed by the local model; role must come only from the user's brief"
    return f"{asset.get('reference', asset['filename'])}: {asset['filename']} ({detail})"


def _effective_system_prompt(
    body: dict[str, Any],
    mode: str,
    flags: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    resolved = flags or {"nsfw": False, "story": False, "variant": None}
    try:
        return resolve_system_prompt(
            mode,
            body.get("system_prompt_override"),
            nsfw=bool(resolved.get("nsfw")),
            story=bool(resolved.get("story")),
            variant=resolved.get("variant"),
        )
    except SystemPromptError as error:
        raise AssemblyError(error.code, error.message) from error


def _validate_reference_tags(text: str, manifest: dict[str, Any], mode: str, field_label: str) -> None:
    if mode != "Reference":
        return
    available = {asset["reference"] for asset in manifest["assets"]}
    canonical_tags = canonical_reference_tags(text)
    missing = sorted(canonical_tags - available)
    if missing:
        tag = missing[0]
        raise AssemblyError(
            "REFERENCE_NOT_FOUND",
            f"{tag} doesn't exist. Add the reference or remove the tag from the {field_label}.",
            {"reference": tag},
        )


def _validated_generation_context(source: dict[str, Any]) -> tuple[float, str, str]:
    duration = source.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0 or duration > 20:
        raise AssemblyError("INVALID_DURATION", "Duration must be between 1 and 20 seconds.")
    aspect_ratio = _required_text(source, "aspect_ratio", "Aspect ratio")
    if aspect_ratio not in ASPECT_RATIOS:
        raise AssemblyError("INVALID_ASPECT_RATIO", "The selected aspect ratio is not supported.")
    brief = _required_text(source, "creative_brief", "Creative brief")
    if len(brief) > 8000:
        raise AssemblyError("BRIEF_TOO_LONG", "Creative brief cannot exceed 8,000 characters.")
    return duration, aspect_ratio, brief


def _validated_music_caption_context(source: dict[str, Any]) -> tuple[str, str]:
    brief = _required_text(source, "creative_brief", "Music brief")
    if len(brief) > 2000:
        raise AssemblyError("BRIEF_TOO_LONG", "Music brief cannot exceed 2,000 characters.")
    lyrics = source.get("lyrics", "")
    if not isinstance(lyrics, str):
        raise AssemblyError("INVALID_REQUEST", "Lyrics must be text.", {"field": "lyrics"})
    lyrics = normalize_unicode_text(lyrics).strip()
    if len(lyrics) > 4000:
        raise AssemblyError("LYRICS_TOO_LONG", "Lyrics cannot exceed 4,000 characters.")
    return brief, lyrics


GUIDE_MESSAGE_NAMES = {
    "base": "official_minimax_h3_guide",
    "reference": "official_minimax_h3_guide",
    "krea2": "official_krea2_prompting_guide",
    "krea2_expansion": "official_krea2_expansion_instructions",
    "anima": "official_anima_prompting_rules",
}


def _guide_content(guide_id: str) -> str:
    """The text of one guide, excerpted where the whole file would be waste."""
    from .guides import anima_prompting_excerpt, krea2_examples_excerpt

    if guide_id == "krea2":
        return krea2_examples_excerpt()
    if guide_id == "anima":
        return anima_prompting_excerpt()
    return load_guide(guide_id)["content"]


def _guide_messages(mode: str, system_prompt: str) -> list[dict[str, str]]:
    if mode == MUSIC3_MODE:
        return ([{"role": "system", "name": "music3_caption_contract", "content": system_prompt}] if system_prompt else [])
    messages = []
    if system_prompt:
        messages.append({"role": "system", "name": "prompt_studio_system_prompt", "content": system_prompt})
    for guide_id in mode_spec(mode).guide_ids:
        messages.append({
            "role": "system",
            "name": GUIDE_MESSAGE_NAMES.get(guide_id, f"official_{guide_id}_guide"),
            "content": _guide_content(guide_id),
        })
    if mode == "Reference":
        messages.append({
            "role": "system",
            "name": "official_minimax_h3_shared_base_rules",
            "content": reference_base_excerpt(),
        })
    return messages


def _final_contract(mode: str, task_text: str) -> str:
    if mode != "Reference":
        mode_rule = {
            "T2VA": "Preserve any explicit continuous-camera or no-cut instruction instead of introducing an unsupported cut.",
            "I2VA": "Separate facts visible in the first frame from newly requested space or action revealed after it.",
            "FL2VA": "Prioritize exact endpoint geometry and a continuous state/camera path between the first and last frames.",
            "L2VA": "Invent only the minimum compatible preceding state needed to reach the final frame; do not infer a named location or period without evidence.",
        }[mode]
        return (
            f"Final grounding check: {mode_rule} "
            "If the brief does not explicitly request non-diegetic music, return N/A for non_diegetic_music. "
            "Return only the complete final MiniMax H3 prompt."
        )
    explicit_edit = bool(re.search(
        r"\b(?:edit(?:ing)?|continue|continuation|extend|remix|re-cut)\b.{0,40}\bvideo\b|\bvideo\s+editing\b",
        task_text,
        re.IGNORECASE | re.DOTALL,
    ))
    task_classification = (
        "source-video editing or continuation; scale detailed_description with source complexity"
        if explicit_edit
        else "reference generation, not keyframe completion or source-video editing"
    )
    return (
        f"Final request classification: {task_classification}. "
        "Treat every explicitly assigned reference role as exclusive unless the user asks that reference to contribute "
        "additional traits; 'only' and 'solely' emphasize this rule but are not required. Unspecified target environment, lighting, "
        "composition, camera treatment, and atmosphere may be designed as new target content, but never described as "
        "facts derived from a reference. Do not add unsupported subject actions, dialogue, props, visible text, or an "
        "invented ending. Music requested without an uploaded audio asset belongs only in non_diegetic_music and must "
        "not create audio-reference or audio-reuse semantics. Prefer one continuous shot unless cuts are requested; "
        "purposeful camera movement within that shot is allowed. Because H3 receives each source video itself, bind the "
        "complete choreography, temporal order, pacing, and rhythmic character of a motion-only video without "
        "reconstructing individual sampled gestures, named steps, poses, expressions, transitions, or a concluding move. "
        "When a concrete visible object, character, scene, or effect from <Video N> is reused in the target, describe that "
        "reused visual element through an appropriate <Subject N> while keeping <Video N> as its source provenance; do not "
        "automatically create a separate subject for ordinary motion transfer. "
        "If the brief does not explicitly request music, non_diegetic_music must be N/A. "
        "Use the official detail budget for grounded target composition, placement, lighting, atmosphere, camera treatment, "
        "supported action progression, and reference application; never pad solely to reach a word count. Return only the complete "
        "prompt with all six required sections in the official order and no commentary outside the prompt."
    )


def _media_inputs(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "asset_id": asset["id"],
            "reference": asset.get("reference"),
            "type": asset["type"],
            "requires_capability": CAPABILITY_BY_TYPE[asset["type"]],
            "frames": [
                {"timestamp": frame["timestamp"], "content_url": frame["url"]}
                for frame in asset.get("frames", [])
            ],
            "content_url": asset["content_url"],
            "visual_width": (
                asset.get("prepared_width") if asset["type"] == "image" else asset.get("contact_sheet_width")
            ),
            "visual_height": (
                asset.get("prepared_height") if asset["type"] == "image" else asset.get("contact_sheet_height")
            ),
        }
        for asset in assets
        if asset["type"] != "audio"
    ]


def _session_manifest(body: dict[str, Any], mode: str, session_id: str) -> dict[str, Any]:
    """The manifest for this compile.

    `session_media` is what the two-pane studio sends: media belongs to the
    session, so it is stored once and projected onto whichever mode is being
    compiled. Without the flag this is the original per-mode manifest, which is
    what the node's own UI and the existing tests use.
    """
    if body.get("session_media"):
        return STORE.view(session_id, mode)
    return STORE.manifest(session_id, mode)


def _image_request(body: dict[str, Any], mode: str, flags: dict[str, Any]) -> dict[str, Any]:
    """Krea 2 and Anima: a still, compiled from the generic prompt or a brief.

    No duration, no reference tags, no soundscape. References are read by the
    prompt model to describe what it sees; the image model itself receives only
    text, so nothing here has to survive as a `<Picture N>` binding.
    """
    target = _target(mode)
    system_prompt, system_prompt_custom = _effective_system_prompt(body, mode, flags)
    doc = _validated_generic(body)
    goals = _validated_goals(body)
    brief = normalize_unicode_text(str(body.get("creative_brief") or "")).strip()
    if len(brief) > target.brief_limit:
        raise AssemblyError("BRIEF_TOO_LONG", f"Creative brief cannot exceed {target.brief_limit:,} characters.")
    if not brief and doc is None:
        raise AssemblyError("INVALID_REQUEST", "Creative brief is required.", {"field": "creative_brief"})
    aspect_ratio = None
    if target.declares("aspect_ratio") and body.get("aspect_ratio"):
        aspect_ratio = _required_text(body, "aspect_ratio", "Aspect ratio")
        if aspect_ratio not in ASPECT_RATIOS:
            raise AssemblyError("INVALID_ASPECT_RATIO", "The selected aspect ratio is not supported.")
    try:
        session_id = parse_session_id(body.get("session_id"))
    except ValueError as error:
        raise AssemblyError("INVALID_SESSION", "The media session ID is invalid.") from error

    manifest = _session_manifest(body, mode, session_id)
    if not manifest["valid"]:
        raise AssemblyError("INVALID_MEDIA_MANIFEST", "The media manifest is not valid.", manifest["violations"])
    references = "\n".join(_media_line(asset) for asset in manifest["assets"]) or "None"
    generic_block = f"Generic prompt:\n{generic_prompt.render(doc)}\n\n" if doc is not None else ""
    brief_block = f"Creative brief:\n{brief}\n\n" if brief else ""
    variant_line = f"Variant: {flags['variant']}\n" if flags.get("variant") else ""
    user_content = (
        f"Target: {target.label}\n"
        f"{variant_line}"
        + (f"Aspect ratio: {aspect_ratio}\n" if aspect_ratio else "")
        + "\nReference media (describe what it actually shows; it is not attached to the image model):\n"
        f"{references}\n\n"
        f"{_established_block(doc, None)}"
        f"{_goal_block(goals)}"
        f"{generic_block}"
        f"{brief_block}"
        + target.final_contract(mode, brief, story=bool(flags.get("story")))
    )
    return {
        "schema_version": 1,
        "guide": {key: value for key, value in load_guide(mode_spec(mode).guide_ids[0]).items() if key != "content"},
        "input": {
            "mode": mode,
            "duration_seconds": None,
            "aspect_ratio": aspect_ratio,
            "creative_brief": brief,
            "media_manifest": manifest,
            "generic": doc,
            "goals": goals,
            # References this mode cannot use were dropped from the view; the
            # user needs to hear that rather than wonder why their picture had
            # no effect.
            "media_warnings": [item["message"] for item in manifest.get("warnings", [])],
            **flags,
        },
        "media_inputs": _media_inputs(manifest["assets"]),
        "supporting_guides": [
            {key: value for key, value in load_guide(guide_id).items() if key != "content"}
            for guide_id in mode_spec(mode).guide_ids[1:]
        ],
        "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
        "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
    }


def assemble_request(body: dict[str, Any]) -> dict[str, Any]:
    mode = _required_text(body, "mode", "Mode")
    flags = _flags(body, mode)
    if _target(mode).workspace == "image":
        return _image_request(body, mode, flags)
    if mode == MUSIC3_MODE:
        system_prompt, system_prompt_custom = _effective_system_prompt(body, mode, flags)
        brief, lyrics = _validated_music_caption_context(body)
        try:
            session_id = parse_session_id(body.get("session_id"))
        except ValueError as error:
            raise AssemblyError("INVALID_SESSION", "The session ID is invalid.") from error
        user_content = (
            f"Music brief:\n{brief}\n\n"
            f"Lyrics:\n{lyrics or 'None provided.'}"
        )
        return {
            "schema_version": 1,
            "guide": {"id": "music3-caption-contract", "title": "MiniMax Music 3 Structured Caption"},
            "input": {
                "mode": mode,
                "duration_seconds": None,
                "aspect_ratio": None,
                "creative_brief": brief,
                "lyrics": lyrics,
                "media_manifest": {"session_id": session_id, "mode": mode, "assets": [], "valid": True},
                **flags,
            },
            "media_inputs": [],
            "supporting_guides": [],
            "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
            "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
        }
    if mode not in MODE_GUIDES:
        raise AssemblyError("INVALID_MODE", "The selected MiniMax mode is not supported.")
    system_prompt, system_prompt_custom = _effective_system_prompt(body, mode, flags)
    doc = _validated_generic(body)
    # With a generic prompt driving the compile the brief is optional: the
    # document already carries everything it was built from, and demanding a
    # brief as well would reject the studio's own request.
    brief = normalize_unicode_text(str(body.get("creative_brief") or "")).strip()
    if not brief and doc is None:
        raise AssemblyError("INVALID_REQUEST", "Creative brief is required.", {"field": "creative_brief"})
    if len(brief) > 8000:
        raise AssemblyError("BRIEF_TOO_LONG", "Creative brief cannot exceed 8,000 characters.")

    aspect_ratio = _required_text(body, "aspect_ratio", "Aspect ratio")
    if aspect_ratio not in ASPECT_RATIOS:
        raise AssemblyError("INVALID_ASPECT_RATIO", "The selected aspect ratio is not supported.")
    duration = body.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0 or duration > 20:
        raise AssemblyError("INVALID_DURATION", "Duration must be between 1 and 20 seconds.")
    try:
        session_id = parse_session_id(body.get("session_id"))
    except ValueError as error:
        raise AssemblyError("INVALID_SESSION", "The media session ID is invalid.") from error

    manifest = _session_manifest(body, mode, session_id)
    if not manifest["valid"]:
        raise AssemblyError("INVALID_MEDIA_MANIFEST", "The media manifest is not valid.", manifest["violations"])

    _validate_reference_tags(brief, manifest, mode, "Creative Brief")
    declared_references = manifest["assets"]
    media_inputs = _media_inputs(declared_references)
    references = "\n".join(_media_line(asset) for asset in declared_references) or "None"
    # Locked facts must constrain generation, not merely grade it afterwards.
    # Auditing alone was measured to fail: with the facts withheld from the
    # request the model writes whatever the brief implies, and a single
    # corrective turn cannot overturn a whole draft built on the wrong subject.
    bible = _validated_bible(body)
    goals = _validated_goals(body)
    established_block = _established_block(doc, bible)
    user_content = (
        f"Mode: {mode}\n"
        f"Duration: {duration:g} seconds\n"
        f"Aspect ratio: {aspect_ratio}\n\n"
        "Reference manifest (audio is not analyzed by the local model; derive its copy/reference role only from the user's words and do not invent its content):\n"
        f"{references}\n\n"
        f"{established_block}"
        f"{_goal_block(goals)}"
        + (f"Creative brief:\n{brief}\n\n" if brief else "")
        + _final_contract(mode, brief)
    )
    guide = guide_for_mode(mode)
    return {
        "schema_version": 1,
        "guide": {key: value for key, value in guide.items() if key != "content"},
        "input": {
            "mode": mode,
            "duration_seconds": duration,
            "aspect_ratio": aspect_ratio,
            "creative_brief": brief,
            "media_manifest": manifest,
            # Optional. Present only once the conversational layer is driving the
            # request; the audit uses it to verify that facts fixed by the user's
            # reference images survived into the generated prompt. Absent means
            # no locks, which is the pre-existing behaviour.
            "bible": bible,
            # The generic prompt document and the standing goal ledger, when the
            # two-pane studio is driving. Both are checked after generation.
            "generic": doc,
            "goals": goals,
            # References this mode cannot use were dropped from the view; the
            # user needs to hear that rather than wonder why their picture had
            # no effect.
            "media_warnings": [item["message"] for item in manifest.get("warnings", [])],
            **flags,
        },
        "media_inputs": media_inputs,
        "supporting_guides": ([{
            key: value for key, value in load_guide("base").items() if key != "content"
        }] if mode == "Reference" else []),
        "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
        "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
    }


def _image_refinement(body: dict[str, Any], mode: str, flags: dict[str, Any]) -> dict[str, Any]:
    """Revise a still's prompt. Same contract as building one, plus the draft.

    Kept separate from the H3 path because that one demands a duration, binds
    `<Audio N>` reference semantics and asks the model for "the complete revised
    H3 prompt" -- instructions that make no sense for a Krea 2 paragraph and that
    its own audit then rejects.
    """
    target = _target(mode)
    system_prompt, system_prompt_custom = _effective_system_prompt(body, mode, flags)
    current_prompt = _required_text(body, "current_prompt", "Current prompt")
    instruction = _required_text(body, "instruction", "Revision instruction")
    if len(current_prompt) > 20_000:
        raise AssemblyError("PROMPT_TOO_LONG", "The current prompt cannot exceed 20,000 characters.")
    if len(instruction) > 2_000:
        raise AssemblyError("INSTRUCTION_TOO_LONG", "The revision instruction cannot exceed 2,000 characters.")
    try:
        session_id = parse_session_id(body.get("session_id"))
    except ValueError as error:
        raise AssemblyError("INVALID_SESSION", "The media session ID is invalid.") from error
    doc = _validated_generic(body)
    goals = _validated_goals(body)
    brief = normalize_unicode_text(str(body.get("creative_brief") or "")).strip()
    manifest = _session_manifest(body, mode, session_id)
    variant_line = f"Variant: {flags['variant']}\n" if flags.get("variant") else ""
    user_content = (
        f"Revise the current {target.label} prompt according to the revision instruction. "
        "Return only the complete revised prompt in the required shape. Do not discuss the changes.\n\n"
        f"Target: {target.label}\n"
        f"{variant_line}\n"
        f"{_established_block(doc, None)}"
        f"{_goal_block(goals)}"
        + (f"Original brief:\n{brief}\n\n" if brief else "")
        + f"Current prompt:\n{current_prompt}\n\n"
        f"Revision instruction:\n{instruction}\n\n"
        + target.final_contract(mode, instruction, story=bool(flags.get("story")))
    )
    return {
        "schema_version": 1,
        "guide": {key: value for key, value in load_guide(mode_spec(mode).guide_ids[0]).items() if key != "content"},
        "input": {
            "mode": mode,
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": brief,
            "current_prompt": current_prompt,
            "instruction": instruction,
            "media_manifest": manifest,
            "generic": doc,
            "goals": goals,
            **flags,
        },
        "media_inputs": [],
        "supporting_guides": [
            {key: value for key, value in load_guide(guide_id).items() if key != "content"}
            for guide_id in mode_spec(mode).guide_ids[1:]
        ],
        "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
        "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
    }


def assemble_refinement(
    body: dict[str, Any],
    cached_generation: dict[str, Any] | None,
) -> dict[str, Any]:
    mode = _required_text(body, "mode", "Mode")
    flags = _flags(body, mode)
    if _target(mode).workspace == "image":
        return _image_refinement(body, mode, flags)
    if mode == MUSIC3_MODE:
        system_prompt, system_prompt_custom = _effective_system_prompt(body, mode)
        current_prompt = _required_text(body, "current_prompt", "Current caption")
        instruction = _required_text(body, "instruction", "Revision instruction")
        if len(current_prompt) > 20_000:
            raise AssemblyError("PROMPT_TOO_LONG", "The current caption cannot exceed 20,000 characters.")
        if len(instruction) > 2_000:
            raise AssemblyError("INSTRUCTION_TOO_LONG", "The revision instruction cannot exceed 2,000 characters.")
        try:
            session_id = parse_session_id(body.get("session_id"))
        except ValueError as error:
            raise AssemblyError("INVALID_SESSION", "The session ID is invalid.") from error
        brief, lyrics = _validated_music_caption_context(body)
        user_content = (
            f"Original music brief:\n{brief}\n\n"
            f"Lyrics:\n{lyrics or 'None provided.'}\n\n"
            f"Current caption:\n{current_prompt}\n\n"
            f"Revision instruction:\n{instruction}\n\n"
            "Revise the current caption according to the revision instruction."
        )
        return {
            "schema_version": 1,
            "guide": {"id": "music3-caption-contract", "title": "MiniMax Music 3 Structured Caption"},
            "input": {
                "mode": mode,
                "duration_seconds": None,
                "aspect_ratio": None,
                "creative_brief": brief,
                "lyrics": lyrics,
                "current_prompt": current_prompt,
                "instruction": instruction,
                "media_manifest": {"session_id": session_id, "mode": mode, "assets": [], "valid": True},
            },
            "media_inputs": [],
            "supporting_guides": [],
            "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
            "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
        }
    if mode not in MODE_GUIDES:
        raise AssemblyError("INVALID_MODE", "The selected MiniMax mode is not supported.")
    system_prompt, system_prompt_custom = _effective_system_prompt(body, mode, flags)
    current_prompt = _required_text(body, "current_prompt", "Current prompt")
    instruction = _required_text(body, "instruction", "Revision instruction")
    if len(current_prompt) > 20_000:
        raise AssemblyError("PROMPT_TOO_LONG", "The current prompt cannot exceed 20,000 characters.")
    if len(instruction) > 2_000:
        raise AssemblyError("INSTRUCTION_TOO_LONG", "The revision instruction cannot exceed 2,000 characters.")
    try:
        session_id = parse_session_id(body.get("session_id"))
    except ValueError as error:
        raise AssemblyError("INVALID_SESSION", "The media session ID is invalid.") from error

    manifest = _session_manifest(body, mode, session_id)
    if not manifest["valid"]:
        raise AssemblyError("INVALID_MEDIA_MANIFEST", "The media manifest is not valid.", manifest["violations"])
    context_source = cached_generation if cached_generation and cached_generation.get("mode") == mode else body
    duration, aspect_ratio, creative_brief = _validated_generation_context(context_source)
    _validate_reference_tags(creative_brief, manifest, mode, "Creative Brief")
    _validate_reference_tags(instruction, manifest, mode, "Revision instruction")
    references = "\n".join(_media_line(asset) for asset in manifest["assets"]) or "None"
    guide = guide_for_mode(mode)
    # A revision is held to the same established facts and standing goals as the
    # generation was; without them a rewrite can drift the locked subject and
    # nothing downstream notices.
    doc = _validated_generic(body)
    goals = _validated_goals(body)
    user_content = (
        "Rewrite the current H3 prompt according to the revision instruction. "
        "Return only the complete revised H3 prompt. Do not discuss the changes.\n\n"
        f"Original mode: {mode}\n"
        f"Original duration: {duration:g} seconds\n"
        f"Original aspect ratio: {aspect_ratio}\n"
        f"Original Creative Brief:\n{creative_brief}\n\n"
        f"Reference manifest (text only; media is intentionally not attached):\n{references}\n\n"
        f"{_established_block(doc, None)}"
        f"{_goal_block(goals)}"
        f"Current prompt:\n{current_prompt}\n\n"
        f"Revision instruction:\n{instruction}\n\n"
        "Reference revision rule: preserve each existing <Audio N> that is absent from the Revision instruction. "
        "Each <Audio N> present in the Revision instruction is mutable in this rewrite: follow the instruction's "
        "meaning to decide whether that reference is present, absent, or changed in the revised prompt. Use only "
        "canonical reference tags listed in the current Reference manifest.\n\n"
        f"{_final_contract(mode, current_prompt + ' ' + instruction)}"
    )
    return {
        "schema_version": 1,
        "guide": {key: value for key, value in guide.items() if key != "content"},
        "input": {
            "mode": mode,
            "duration_seconds": duration,
            "aspect_ratio": aspect_ratio,
            "creative_brief": creative_brief,
            "current_prompt": current_prompt,
            "instruction": instruction,
            "media_manifest": manifest,
            "generic": doc,
            "goals": goals,
            **flags,
        },
        "media_inputs": [],
        "supporting_guides": ([{
            key: value for key, value in load_guide("base").items() if key != "content"
        }] if mode == "Reference" else []),
        "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
        "messages": _guide_messages(mode, system_prompt) + [{"role": "user", "content": user_content}],
    }


def assemble_lyrics_request(body: dict[str, Any]) -> dict[str, Any]:
    mode = _required_text(body, "mode", "Mode")
    if mode != MUSIC3_MODE:
        raise AssemblyError("INVALID_MODE", "Lyrics rewriting is available only in Music 3.")
    try:
        system_prompt, system_prompt_custom = resolve_system_prompt(
            "Music3Lyrics",
            body.get("system_prompt_override"),
        )
    except SystemPromptError as error:
        raise AssemblyError(error.code, error.message) from error

    current_lyrics = body.get("current_lyrics", "")
    instruction = body.get("instruction", "")
    use_music_brief = body.get("use_music_brief", True)
    brief = body.get("creative_brief", "")
    if not isinstance(current_lyrics, str):
        raise AssemblyError("INVALID_REQUEST", "Current Lyrics must be text.", {"field": "current_lyrics"})
    if not isinstance(instruction, str):
        raise AssemblyError("INVALID_REQUEST", "Revision instruction must be text.", {"field": "instruction"})
    if not isinstance(use_music_brief, bool):
        raise AssemblyError("INVALID_REQUEST", "Use Music Brief must be a boolean.", {"field": "use_music_brief"})
    if not isinstance(brief, str):
        raise AssemblyError("INVALID_REQUEST", "Music Brief must be text.", {"field": "creative_brief"})
    current_lyrics = normalize_unicode_text(current_lyrics).strip()
    instruction = normalize_unicode_text(instruction).strip()
    brief = normalize_unicode_text(brief).strip() if use_music_brief else ""
    if len(current_lyrics) > 4000:
        raise AssemblyError("LYRICS_TOO_LONG", "Lyrics cannot exceed 4,000 characters.")
    if len(instruction) > 2000:
        raise AssemblyError("INSTRUCTION_TOO_LONG", "The revision instruction cannot exceed 2,000 characters.")
    if len(brief) > 2000:
        raise AssemblyError("BRIEF_TOO_LONG", "Music brief cannot exceed 2,000 characters.")
    if current_lyrics and not instruction:
        raise AssemblyError("INSTRUCTION_REQUIRED", "Describe how the existing Lyrics should change.")
    if not current_lyrics and not instruction and not brief:
        raise AssemblyError("LYRICS_REQUEST_EMPTY", "Add an instruction or include the Music Brief to create Lyrics.")
    try:
        session_id = parse_session_id(body.get("session_id"))
    except ValueError as error:
        raise AssemblyError("INVALID_SESSION", "The session ID is invalid.") from error

    task = "Rewrite the Current Lyrics according to the revision instruction." if current_lyrics else "Create complete new Lyrics."
    user_content = (
        f"Task: {task}\n\n"
        f"Music Brief:\n{brief or 'Not included.'}\n\n"
        f"Current Lyrics:\n{current_lyrics or 'None provided.'}\n\n"
        f"Revision instruction:\n{instruction or 'None provided.'}"
    )
    return {
        "schema_version": 1,
        "guide": {"id": "music3-lyrics-contract", "title": "MiniMax Music 3 Lyrics"},
        "input": {
            "mode": mode,
            "target": "lyrics",
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": brief,
            "current_lyrics": current_lyrics,
            "instruction": instruction,
            "use_music_brief": use_music_brief,
            "media_manifest": {"session_id": session_id, "mode": mode, "assets": [], "valid": True},
        },
        "media_inputs": [],
        "supporting_guides": [],
        "system_prompt": {"custom": system_prompt_custom, "content": system_prompt},
        "messages": ([{"role": "system", "name": "music3_lyrics_contract", "content": system_prompt}] if system_prompt else [])
        + [{"role": "user", "content": user_content}],
    }
