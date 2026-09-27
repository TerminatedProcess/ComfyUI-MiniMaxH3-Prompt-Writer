"""Building and steering the generic prompt: two model calls and their contracts.

Both go through the same runtime as a normal generation (single_call policy, so
the H3 audit does not run on them) and both return JSON, not prose:

    build   brief + reference media  ->  observation of the media + the scene
    turn    one chat message         ->  a reply, a field patch, a re-observation
                                         and any new standing goals

Origins are assigned HERE, deterministically, from where a value came from
structurally -- never by asking the model to classify its own inputs. That was
measured at 0/6 on this stack, and it erred toward marking the user's own words
as invented, which would let them be silently overwritten later.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import generic, heat as heat_levels, goals as goal_ledger
from .scene_bible import distinctive_tokens
from .text_normalization import normalize_unicode_text

FIELD_LIST = ", ".join(generic.FIELDS)
SCENE_FIELD_LIST = ", ".join(generic.SCENE_FIELDS)
PERSON_FIELD_LIST = ", ".join(generic.PERSON_FIELDS)
RELATION_LIST = ", ".join(f'"{name}" ({meaning})' for name, meaning in generic.RELATIONS.items())
# How a second person is addressed. Spelled out for the model, because the whole
# point is that an attribute belongs to somebody.
PEOPLE_RULE = (
    "PEOPLE. Count the people first, then write one \"people\" object for EACH of them.\n"
    f"- Each object may hold {', '.join(chr(34) + name + chr(34) for name in generic.PERSON_FIELDS)} -- that "
    "person's own, never anybody else's. A man's tights do not go in the woman's wardrobe.\n"
    f"- Up to {generic.MAX_PEOPLE} people. The first is A, the second B, the third C, and a letter always means "
    "the same body everywhere in the answer.\n"
    "- NEVER describe two people in one field. \"two dancers\" as a subject is wrong: give each their own object.\n"
    "- One person alone: still use \"people\", with a single object in it.\n"
    "- Only use a letter in \"relations\" if you wrote that person's object in this answer."
)

BUILD_INSTRUCTIONS = f"""You are building a model-agnostic scene document for an image or video prompt. Return only JSON.

Use exactly this shape:
{{"observed": {{"<scene field>": "<what the reference media actually shows>"}}, "scene": {{"<scene field>": "<the fact for the finished shot>"}}, "people": [{{"<person field>": "<about that one person>"}}], "from_brief": {{"<field>": "<the exact words copied from the user's brief that this field came from>"}}, "relations": [{{"from": "<A|B|a noun>", "rel": "<relation>", "to": "<A|B|a noun>", "qualifier": "<optional, <=5 words>"}}]}}

"people" is ONE OBJECT PER PERSON, in frame order. Everything inside an object belongs to that person and nobody else:
{{"people": [{{"subject": "a woman in her 20s", "wardrobe": "a white leotard", "pose": "curled up, knees drawn in"}},
            {{"subject": "a man in his 30s", "wardrobe": "white tights, no shirt", "pose": "kneeling on one knee"}}],
 "relations": [{{"from": "A", "rel": "on", "to": "B", "qualifier": "on his shoulder"}}]}}
The first object is A, the second is B, the third is C. Check each object before you finish: everything in it must describe the SAME body.

Scene fields, for "observed" and "scene": {SCENE_FIELD_LIST}.
Person fields, for each object in "people": {PERSON_FIELD_LIST}.

{PEOPLE_RULE}
"relations" is a list of how things sit against each other: [{{"from": "A", "rel": "on", "to": "the couch"}}, {{"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap"}}].
- "from" and "to" are a person letter (A, B, C) or a plain noun ("the couch", "the desk").
- "rel" is EXACTLY one of: {RELATION_LIST}.
- "occludes" means "from where the camera is, this hides part of that" -- it is how you say which thing is in front. "left_of" and "right_of" are as the camera sees them, not the subject's own left and right.
- "qualifier" is at most five words for what the relation alone cannot say ("across her lap", "by the shoulders").
- Only relations you can see or that the user stated. Do not relate every object in the room: the people, what they touch, and what sits in front of or behind them.

Rules:
- "observed" describes ONLY what is literally visible or audible in the supplied reference media. Omit a field you cannot see. Never guess there. If no media is supplied, return an empty object.
- "scene" is the shot to make: the user's brief first, the reference media second, your own judgement last.
- ONE short factual clause per field: a phrase or a single sentence, at most 25 words. Not a paragraph. No lists, no hedging, no "maybe".
- Colours, materials, counts and spatial relationships belong in the field they describe; be specific about them.
- "pose" is the body, and it is never left to the reader. Write it as: one of standing, sitting, perched, leaning, kneeling, crouching, lying, walking -- then which way they face the camera (facing the camera, three-quarter, in profile, from behind) -- then what holds them up if anything ("on a wooden chair", "against the desk edge"). Example: "sitting, three-quarter to the camera, on a wooden chair".
- "staging" places everything else around the subject, relative to the CAMERA, because "in front of the desk" does not say from whose side. Name what is behind the subject, what is between the subject and the camera, and what is to each side, and give the camera's height and distance if you can see them. Example: "desk behind her, window behind the desk, camera at eye level a few feet away". Only what the media actually shows.
- When reference media is supplied, "pose" and "staging" are READ OFF IT, never inferred from how the brief phrases things: a brief saying "at her desk" says nothing about whether she is sitting.
- "exclusions" lists what must NOT appear.
- "from_brief" is how a field is credited to the user rather than to you. When a field is based on something the user actually wrote, copy that fragment of their brief VERBATIM -- their characters, not a paraphrase. Omit the field if you expanded or invented it. Copying words they did not write does not make the field theirs; it is checked against the brief.
- Leave a field out of "scene" entirely rather than writing "unknown", "none" or "not specified".
- No commentary, no Markdown, no code fences, no extra keys."""

STORY_BUILD_CLAUSE = (
    "Story builder is ON: fill every field you reasonably can, inventing specific supporting detail -- staging, "
    "wardrobe, light, weather, mood, camera -- so the document describes one deliberate shot rather than a sketch. "
    "Never contradict the brief or the reference media."
)
FAITHFUL_BUILD_CLAUSE = (
    "Story builder is OFF: fill only the fields the brief or the reference media actually support, and leave "
    "everything else out."
)
NSFW_EXPAND_CLAUSE = (
    "Naughty is ON, and expanding is where it applies: develop the sensual or explicit dimension of this scene "
    "rather than only permitting it. Bring out the body, the wardrobe and how it sits, the contact between "
    "people, the charge in the look and the mood, and say it plainly, without euphemism or moralising. Stay "
    "inside what the user wrote and what any reference shows -- heighten their scene, never replace it with a "
    "different one, and never contradict a fact they gave."
)
NSFW_BUILD_CLAUSE = (
    "Adult or explicit content is permitted where the brief calls for it; describe it plainly, without euphemism "
    "or refusal."
)

TURN_INSTRUCTIONS = f"""You are steering a model-agnostic scene document through conversation. Return only JSON.

Use exactly this shape:
{{"reply": "<one short sentence to the user>", "patch": {{"<field>": "<new value>"}}, "observed": {{"<field>": "<what the reference media actually shows>"}}, "relations": [{{"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap"}}], "goals": [{{"text": "<standing instruction>", "kind": "field|presence|judged", "fields": ["<field>"], "must_include": ["<exact text>"]}}]}}

Fields: {FIELD_LIST}.

{PEOPLE_RULE}

Rules:
- "patch" carries facts the user just stated or changed. Only fields they actually addressed. Their words win over the reference media.
- "relations" replaces how things sit against each other, same shape as the build: [{{"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap"}}]. Send it when the user corrects an arrangement, and omit it otherwise.
- A correction about the body ("she is standing, not sitting", "she is facing away") is a "pose" patch; one about where things sit relative to the camera ("the desk is behind her", "the window is behind the viewer") is a "staging" patch. Neither belongs in "action", which is what she is DOING.
- "observed" is for when the user says the document is not following the reference media: re-read the supplied media and report what it ACTUALLY shows for the fields in question. Omit it otherwise, and never put a guess in it.
- "goals" carries standing instructions -- a rule that must keep holding from now on ("always follow the clothing colours in the image", "never mention a brand", "keep her jacket red"). A one-off fact correction is a patch, NOT a goal. A request can be both.
  - kind "field" when the rule is that specific fields must come from the reference media: list them in "fields".
  - kind "presence" when a named thing must appear in every prompt: put the exact wording in "must_include".
  - kind "judged" for anything else.
- Omit any key you are not using. Keep "reply" to one sentence, plain and specific, with no restating of the whole document.
- No commentary outside the JSON, no Markdown, no code fences."""

MAX_MESSAGE_CHARS = 2000
# Both generic stages return JSON. At the writer's creative sampling a 19-field
# document intermittently stops mid-string -- observed against the local
# qwen3-vl-8b -- and a document that does not close is a total loss, unlike a
# prompt that merely reads oddly.
STRUCTURED_SAMPLING = {"temperature": 0.6, "top_p": 0.9, "top_k": 40}
# Below this, a salvaged document counts as a failed generation rather than a
# result, so the build retries instead of returning a near-empty card.
MIN_SALVAGED_FIELDS = 5
# How many words may sit between two of the user's words before their phrase stops
# counting as theirs.
PHRASE_GAP = 2
# How much of a value has to be present in the user's own text before the value
# counts as their words. Deliberately high: marking an invention as the user's
# locks it into every future compile, which is the expensive mistake.
USER_TOKEN_THRESHOLD = 0.6


class ConversationError(ValueError):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _recover_truncated_object(value: str) -> dict[str, Any] | None:
    """Rebuild the complete part of a JSON object that was cut off mid-write.

    Small local models -- the ones this writer is built for -- intermittently emit
    a stop token in the middle of a long object; measured at roughly one answer in
    three against a local abliterated qwen3-vl-8b. Everything before the cut is
    perfectly good, so discarding it costs the user another twelve seconds for
    fields the model already wrote correctly.

    Only whole key/value pairs survive: the partial pair at the cut is dropped, and
    a result that still will not parse is refused rather than guessed at.
    """
    depth = 0
    in_string = False
    escaped = False
    # Offsets just past a completed top-level pair, i.e. after its closing brace
    # or quote, where the object can be closed off cleanly.
    boundaries: list[int] = []
    for index, character in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                if depth == 2:
                    boundaries.append(index + 1)
        elif character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
            if depth == 1:
                boundaries.append(index + 1)
    for cut in reversed(boundaries):
        candidate = value[:cut].rstrip().rstrip(",")
        for suffix in ("}}", "}"):
            try:
                parsed = json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _parse_json_object(text: str, code: str, message: str) -> tuple[dict[str, Any], bool]:
    """Read the model's JSON, and report what it actually said when it is not JSON.

    The snippet matters: "the model did not return a usable document" is the same
    message whether the model refused, narrated, or ran out of room, and without
    its own words there is nothing to act on.
    """
    value = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", value)
    if fence:
        value = fence.group(1)

    def fail(reason: str) -> ConversationError:
        return ConversationError(code, message, {
            "reason": reason,
            "model_said": (text or "")[:600],
            "characters": len(text or ""),
        })

    start = value.find("{")
    if start < 0:
        raise fail("empty_output" if not value else "no_json_object")
    tail = value[start:]
    # Greedy-to-the-last-brace handles an answer with commentary after the object;
    # the raw tail is what a TRUNCATED answer needs, because its last brace may
    # belong to an inner object that closed long before the cut.
    candidates = [tail]
    match = re.search(r"\{[\s\S]*\}", tail)
    if match and match.group(0) != tail:
        candidates.insert(0, match.group(0))
    error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as failure:
            error = error or failure
            continue
        if isinstance(parsed, dict):
            return parsed, False
        raise fail("not_an_object")
    recovered = _recover_truncated_object(tail)
    if recovered is not None:
        # Salvaged: the caller decides whether what survived is enough.
        return recovered, True
    raise fail(
        f"invalid_json at line {error.lineno} column {error.colno}" if error else "invalid_json"
    )


def _clean_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not generic.is_field(key) or not isinstance(value, str):
            continue
        text = normalize_unicode_text(value).strip()
        if not text or text.lower() in {"none", "unknown", "n/a", "not specified", "unspecified", "null"}:
            continue
        result[key] = text[: generic.MAX_FIELD_CHARS]
    return result


def _ordered_tokens(text: str) -> list[str]:
    keep = distinctive_tokens(text)
    return [token for token in re.findall(r"[a-z0-9']+", (text or "").lower()) if token in keep]


def brief_supports(value: str, brief: str) -> bool:
    """Whether this field carries a phrase the user actually wrote.

    Measured against the naive direction (how much of the value appears in the
    brief): with Story builder on, a four-word request becomes a forty-word field,
    so the overlap collapses and every fact the user asked for is filed as
    invented -- and invented facts are never locked. Matching the user's own
    PHRASES inside the value survives expansion, while a single shared word
    ("blue" reaching a camera field from "blue hour") is not enough to lock it.
    """
    brief_tokens = _ordered_tokens(brief)
    if len(brief_tokens) < 2:
        return False
    value_tokens = _ordered_tokens(value)
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(value_tokens):
        positions.setdefault(token, []).append(index)
    # A small gap is allowed because expansion inserts adjectives: the user's
    # "blue skirt" comes back as "cerulean blue silk skirt", which is still their
    # phrase. Two unrelated words landing near each other by chance is far less
    # likely than that, and the cost of a miss is their own fact left unlocked.
    for index in range(len(brief_tokens) - 1):
        first, second = brief_tokens[index], brief_tokens[index + 1]
        for start in positions.get(first, ()):
            if any(start < follow <= start + PHRASE_GAP + 1 for follow in positions.get(second, ())):
                return True
    return False


def _normalized_for_quote(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quoted_from_brief(quote: Any, brief: str) -> bool:
    """Whether a claimed quote really is the user's own wording.

    The model is allowed to say which words a field came from, but not to be
    believed: the quote has to appear in the brief verbatim. That keeps the
    signal structural -- self-classification was measured at 0/6 on this stack --
    while still crediting a field the user wrote and the model then expanded.

    Without it, an expanded field ("a bicycle courier" becoming forty words of
    scene) shares too few tokens with the brief to pass `brief_supports`, and the
    user's own facts are filed as invented and never locked.
    """
    if not isinstance(quote, str):
        return False
    text = _normalized_for_quote(quote)
    if len(text) < 4:
        return False
    return text in _normalized_for_quote(brief)


def build_instructions(*, nsfw: bool = True, story: bool, heat: int | None = None) -> str:
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    parts = [BUILD_INSTRUCTIONS, STORY_BUILD_CLAUSE if story else FAITHFUL_BUILD_CLAUSE]
    parts.append(heat_levels.clause(level))
    # Story builder off used to cap the dial here, so a user asking for Explicit
    # quietly got Allowed. The dial is the user talking: it develops what it
    # names, and Story builder still governs everything else.
    if not story and heat_levels.develops_adult(level):
        parts.append(HEAT_OVERRIDES_FAITHFUL)
    return "\n\n".join(parts)


def turn_instructions(*, nsfw: bool = True, story: bool, heat: int | None = None) -> str:
    parts = [TURN_INSTRUCTIONS]
    if story:
        parts.append(
            "Story builder is ON, so you may also fill fields the user has not addressed when the change implies "
            "them -- but never overwrite a fact they gave."
        )
    parts.append(heat_levels.clause(heat_levels.resolve(heat=heat, nsfw=nsfw)))
    return "\n\n".join(parts)


def assemble_build(
    *,
    session_id: str,
    brief: str,
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    doc: dict[str, Any] | None,
    goals: list[dict[str, Any]],
    nsfw: bool = True,
    heat: int | None = None,
    story: bool,
    duration_seconds: float | None,
    aspect_ratio: str | None,
) -> dict[str, Any]:
    """One request that reads the references and writes the whole document."""
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    instructions = build_instructions(heat=level, story=story)
    references = "\n".join(
        f"{asset.get('reference') or asset.get('filename')}: {asset.get('filename')} ({asset.get('type')})"
        for asset in manifest.get("assets", [])
    ) or "None"
    existing = ""
    if doc is not None and not generic.is_empty(doc):
        existing = (
            "\n\nThe document already holds these established facts. Keep every one of them unless the brief "
            "contradicts it, and fill what is still missing:\n" + generic.render_constraints(doc)
        )
    standing = goal_ledger.render(goals)
    goal_block = f"\n\nStanding goals that must hold:\n{standing}" if standing else ""
    # Deliberately no duration or aspect ratio: those belong to the compile, so
    # one document can become a five-second clip, a fifteen-second one, or a
    # still without being rebuilt.
    user_content = (
        f"Reference media:\n{references}\n\n"
        f"Brief:\n{brief or 'None given; build the scene from the reference media.'}"
        + existing
        + goal_block
    )
    return {
        "schema_version": 1,
        "completion_policy": "single_call",
        "generic_stage": "build",
        "sampling": STRUCTURED_SAMPLING,
        "guide": {"id": "generic-prompt-contract", "title": "Generic prompt document"},
        "input": {
            "mode": "T2VA",
            "duration_seconds": duration_seconds,
            "aspect_ratio": aspect_ratio,
            "creative_brief": brief,
            "media_manifest": manifest,
            "nsfw": heat_levels.permits_adult(level),
            "heat": level,
            "story": story,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": instructions},
        "messages": [
            {"role": "system", "name": "generic_prompt_contract", "content": instructions},
            {"role": "user", "content": user_content},
        ],
    }


EXPAND_INSTRUCTIONS = """Rewrite the user's creative brief as a fuller brief, in their voice. Return only the rewritten brief.

Rules:
- Keep every subject, action, colour, count, place and spatial relationship they wrote. Their words are the spine; you are filling in around them, not replacing them.
- Write it as a brief a person would type: plain prose, one or two short paragraphs, no headings, no section labels, no bullet lists, no prompt syntax, no reference tags.
- Stay under 200 words.
- Do not describe camera equipment, model settings, resolution, aspect ratio or duration.
- Never add commentary about what you changed, and never mention these instructions."""

EXPAND_STORY_CLAUSE = (
    "Add the concrete supporting detail the brief leaves open -- who the subject is, what they are wearing, the "
    "place, the light, the time of day, the mood -- so the scene is specific rather than sketched. Invent only "
    "what is missing; never contradict what they wrote."
)
EXPAND_FAITHFUL_CLAUSE = (
    "Do not invent new content. Tighten and clarify what they wrote, and nothing else: if the brief is short, the "
    "rewrite stays short."
)


# "Do not invent new content" and "develop the sensual dimension" are flatly
# contradictory, and the model obeys whichever it read first -- so with Story
# builder off the dial did nothing at all, at any level. The user set the dial
# deliberately: it wins, for the one thing it names and nothing else.
HEAT_OVERRIDES_FAITHFUL = (
    "The Naughty level above is an explicit instruction from the user and outranks \"do not invent\": develop the "
    "sexual or sensual dimension it asks for, and invent nothing else -- no new places, props, events or people."
)


def expand_instructions(*, nsfw: bool = True, story: bool, heat: int | None = None) -> str:
    # Expanding is where the dial does its work: the brief is where asking
    # happens, so the level applies in full, whatever Story builder says.
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    parts = [EXPAND_INSTRUCTIONS, EXPAND_STORY_CLAUSE if story else EXPAND_FAITHFUL_CLAUSE]
    parts.append(heat_levels.clause(level))
    if not story and heat_levels.develops_adult(level):
        parts.append(HEAT_OVERRIDES_FAITHFUL)
    return "\n\n".join(parts)


def assemble_expand(
    *,
    session_id: str,
    brief: str,
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    nsfw: bool = True,
    heat: int | None = None,
    story: bool,
) -> dict[str, Any]:
    """Expand the brief itself, on demand and visibly.

    Deliberately NOT what the flags do on their own: a switch that silently
    rewrote your brief would make every invention look like your own words, and
    the document credits -- and therefore locks -- exactly the facts that carry
    your phrasing. Here you read the result, edit it, or undo it, and it is
    yours because you accepted it.
    """
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    instructions = expand_instructions(heat=level, story=story)
    references = "\n".join(
        f"{asset.get('reference') or asset.get('filename')}: {asset.get('filename')} ({asset.get('type')})"
        for asset in manifest.get("assets", [])
    ) or "None"
    user_content = (
        f"Reference media {'attached to this message' if media_inputs else '(none)'}:\n{references}\n\n"
        f"Their brief:\n{brief}"
    )
    return {
        "schema_version": 1,
        "completion_policy": "single_call",
        "generic_stage": "expand",
        "sampling": STRUCTURED_SAMPLING,
        "guide": {"id": "brief-expansion", "title": "Creative brief expansion"},
        "input": {
            "mode": "T2VA",
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": brief,
            "media_manifest": manifest,
            "nsfw": heat_levels.permits_adult(level),
            "heat": level,
            "story": story,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": instructions},
        "messages": [
            {"role": "system", "name": "brief_expansion_contract", "content": instructions},
            {"role": "user", "content": user_content},
        ],
    }


DESCRIBE_INSTRUCTIONS = """Look at the attached reference image and write the user's creative brief for it. Return only the brief.

Rules:
- Describe what is actually in the picture: the subject, what they are wearing and doing, where they are, the light, the time of day, the mood, and how the shot is framed.
- Say plainly whether the subject is standing, sitting, kneeling, lying or leaning, which way they face the camera, and what is behind them versus between them and the camera. A reader must not have to guess the body or the layout.
- Write it as a brief a person would type: plain prose, one or two short paragraphs, no headings, no section labels, no bullet lists, no prompt syntax, no reference tags.
- Stay under 200 words.
- Do not describe camera equipment, model settings, resolution, aspect ratio or duration, and never name a file, an artist or a model.
- Never add commentary about the image or about these instructions, and never say "this image shows"; write the scene itself."""

DESCRIBE_STORY_CLAUSE = (
    "This brief is for a video that starts from this frame, so add the motion the still implies -- what the "
    "subject does next, how the scene moves around them. Keep it to what the picture plausibly leads into; never "
    "contradict what is visible."
)
DESCRIBE_FAITHFUL_CLAUSE = (
    "Describe only what is visible. Do not invent a backstory, a name, an action the picture does not show, or "
    "anything outside the frame."
)


def describe_instructions(*, nsfw: bool = True, story: bool, heat: int | None = None) -> str:
    parts = [DESCRIBE_INSTRUCTIONS, DESCRIBE_STORY_CLAUSE if story else DESCRIBE_FAITHFUL_CLAUSE]
    parts.append(heat_levels.clause(heat_levels.resolve(heat=heat, nsfw=nsfw)))
    return "\n\n".join(parts)


def assemble_describe(
    *,
    session_id: str,
    asset: dict[str, Any],
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    nsfw: bool = True,
    heat: int | None = None,
    story: bool,
) -> dict[str, Any]:
    """Read ONE reference image and write a brief from it.

    Deliberately single-asset: the studio triggers this by double-clicking a
    specific picture, and sending the whole media set would describe a scene the
    user did not point at. The manifest and media_inputs are narrowed to that
    asset by the caller.
    """
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    instructions = describe_instructions(heat=level, story=story)
    label = asset.get("reference") or asset.get("filename")
    return {
        "schema_version": 1,
        "completion_policy": "single_call",
        "generic_stage": "describe",
        "sampling": STRUCTURED_SAMPLING,
        "guide": {"id": "brief-from-image", "title": "Creative brief from a reference image"},
        "input": {
            "mode": "T2VA",
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": "",
            "media_manifest": manifest,
            "nsfw": heat_levels.permits_adult(level),
            "heat": level,
            "story": story,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": instructions},
        "messages": [
            {"role": "system", "name": "brief_from_image_contract", "content": instructions},
            {
                "role": "user",
                "content": f"The reference image attached to this message is {label}. Write the brief for it.",
            },
        ],
    }


def read_json_object(text: str) -> dict[str, Any]:
    """A model answer as a dict, or a ConversationError naming what it said."""
    parsed, _salvaged = _parse_json_object(text, "INVALID_JSON", "The model did not answer with JSON.")
    return parsed


POSE_PROBE_INSTRUCTIONS = """You are reading evidence off a picture. Return only JSON, no prose.

Answer every key. Judge ONLY what the picture shows; if something is not visible, say so with false or "none visible".

Never describe the pose, never name it, and never explain your answer. Report what is there."""


def assemble_pose_probe(
    *,
    session_id: str,
    asset: dict[str, Any],
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask a picture what is visible, never what the pose is.

    The conclusion question is the one the model gets wrong: asked whether a
    subject was standing or sitting it answered "sitting" from her torso height,
    and answered it again when the choice was a fixed menu. Asked whether the
    chair seat was occupied and where her body was relative to the desk edge, the
    same model on the same picture got all of it right. See backend/pose_probe.py.
    """
    from . import pose_probe

    questions = "\n".join(f"- {key}: {text}" for key, text in pose_probe.QUESTIONS.items())
    label = asset.get("reference") or asset.get("filename")
    return {
        "schema_version": 1,
        "completion_policy": "single_call",
        "generic_stage": "pose_probe",
        "sampling": STRUCTURED_SAMPLING,
        "guide": {"id": "pose-evidence", "title": "Pose evidence probe"},
        "input": {
            "mode": "T2VA",
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": "",
            "media_manifest": manifest,
            "nsfw": True,
            "story": False,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": POSE_PROBE_INSTRUCTIONS},
        "messages": [
            {"role": "system", "name": "pose_probe_contract", "content": POSE_PROBE_INSTRUCTIONS},
            {
                "role": "user",
                "content": (
                    f"Read the person in {label}.\n\nUse exactly this shape:\n{pose_probe.schema_line()}\n\n"
                    f"What each key means:\n{questions}"
                ),
            },
        ],
    }


MAX_BRIEF_CHARS = 8000


def clean_expanded_brief(text: str) -> str:
    """Take the rewritten brief, refusing anything that is not one.

    A model that answers with a section-headed prompt, a bullet list or a
    preamble has not written a brief, and pasting that into the box would be
    worse than leaving the original alone.
    """
    value = normalize_unicode_text(text or "").strip()
    fence = re.fullmatch(r"```(?:[a-zA-Z]*)\s*\n([\s\S]*?)\n```", value)
    if fence:
        value = fence.group(1).strip()
    if not value:
        raise ConversationError("INVALID_BRIEF_EXPANSION", "The model returned an empty brief.")
    if re.search(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", value):
        raise ConversationError(
            "INVALID_BRIEF_EXPANSION",
            "The model returned a list or a heading instead of a brief.",
            {"model_said": value[:600]},
        )
    if re.search(r"(?im)^\s*(?:subject_definitions|detailed_description|overall_soundscape|positive)\s*:", value):
        raise ConversationError(
            "INVALID_BRIEF_EXPANSION",
            "The model returned a finished prompt instead of a brief.",
            {"model_said": value[:600]},
        )
    return value[:MAX_BRIEF_CHARS]


def assemble_turn(
    *,
    session_id: str,
    message: str,
    doc: dict[str, Any],
    goals: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    nsfw: bool = True,
    heat: int | None = None,
    story: bool,
) -> dict[str, Any]:
    """One conversation turn.

    The media is re-attached every turn when any is loaded: "you are not
    following the colours in the image" cannot be answered from the document
    alone, and asking the user to say which kind of correction they meant would
    put the mechanism in their way.
    """
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    instructions = turn_instructions(heat=level, story=story)
    records = generic.records(doc)
    document = {
        key: {
            "value": record["value"],
            "origin": record["origin"],
            **({"observed_in_reference": record["observed"]} if record["observed"] else {}),
        }
        for key, record in records.items()
        if record["value"]
    }
    history = [
        f"{'You' if turn['role'] == 'assistant' else 'User'}: {turn['text']}"
        for turn in (conversation or [])[-8:]
    ]
    references = "\n".join(
        f"{asset.get('reference') or asset.get('filename')}: {asset.get('filename')} ({asset.get('type')})"
        for asset in manifest.get("assets", [])
    ) or "None"
    standing = goal_ledger.render(goals)
    user_content = (
        f"Current document (origin 'user' and 'override' are the user's own choices; 'override' deliberately "
        f"differs from the reference media and must never be reverted):\n"
        f"{json.dumps(document, ensure_ascii=False, indent=1)}\n\n"
        f"Reference media {'attached to this message' if media_inputs else '(none loaded)'}:\n{references}\n\n"
        + (f"Standing goals:\n{standing}\n\n" if standing else "")
        + (("Recent conversation:\n" + "\n".join(history) + "\n\n") if history else "")
        + f"User says:\n{message}"
    )
    return {
        "schema_version": 1,
        "completion_policy": "single_call",
        "generic_stage": "turn",
        "sampling": STRUCTURED_SAMPLING,
        "guide": {"id": "generic-prompt-turn", "title": "Generic prompt conversation"},
        "input": {
            "mode": "T2VA",
            "duration_seconds": None,
            "aspect_ratio": None,
            "creative_brief": message,
            "media_manifest": manifest,
            "nsfw": heat_levels.permits_adult(level),
            "heat": level,
            "story": story,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": instructions},
        "messages": [
            {"role": "system", "name": "generic_prompt_turn_contract", "content": instructions},
            {"role": "user", "content": user_content},
        ],
    }


def apply_build(
    doc: dict[str, Any],
    text: str,
    *,
    brief: str,
    has_media: bool,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Fold a build answer into the document, deciding each field's origin.

    Structural, not self-reported:
      seen in the reference           -> asset
      seen, but the brief says other  -> override, keeping what was seen
      not seen, and the brief says it -> user
      not seen, nobody said it        -> invented
    """
    parsed, salvaged = _parse_json_object(
        text,
        "INVALID_GENERIC_BUILD",
        "The model did not return a usable scene document. Try Generate again.",
    )
    raw_scene = parsed.get("scene") if isinstance(parsed.get("scene"), dict) else {}
    raw_observed = parsed.get("observed") if isinstance(parsed.get("observed"), dict) else {}
    observed = _clean_map(raw_observed) if has_media else {}
    scene = _clean_map(raw_scene)
    # "people" is the preferred shape; suffixed keys sent directly still work.
    # Taken from wherever the model put the list: asked for it at the top level
    # it nested it under "scene" instead, which is a reasonable place for it and
    # not worth losing a good answer over.
    crowd = _clean_map(_people_map(
        parsed.get("people") or raw_scene.get("people") or raw_observed.get("people")
    ))
    scene = {**crowd, **scene}
    if has_media:
        # Read off the picture, so they lock like any other observation.
        observed = {**crowd, **observed}
    claimed = parsed.get("from_brief") if isinstance(parsed.get("from_brief"), dict) else {}
    if not scene and not observed:
        raise ConversationError(
            "INVALID_GENERIC_BUILD",
            "The model returned no scene fields. Try Generate again, or add more to the brief.",
            {"reason": "no_fields", "model_said": (text or "")[:600]},
        )
    if salvaged and len(scene) + len(observed) < MIN_SALVAGED_FIELDS:
        # Salvage is a rescue, not a result. A document cut after one or two
        # fields is not worth keeping: the user asked for a scene, and quietly
        # handing back a near-empty card looks like the writer ignored them.
        raise ConversationError(
            "INVALID_GENERIC_BUILD",
            "The model stopped after only a couple of fields. Try Generate again.",
            {
                "reason": "truncated_early",
                "recovered_fields": len(scene) + len(observed),
                "model_said": (text or "")[:600],
            },
        )
    result = doc
    changed: list[str] = []
    protected: list[str] = []
    # The union: the model may introduce a second person this turn, and those
    # keys are not in the document yet.
    for key in sorted({*generic.doc_fields(doc), *observed, *scene}, key=generic.sort_key):
        seen = observed.get(key)
        stated = scene.get(key)
        value = stated or seen
        if not value:
            continue
        current = generic.record(result, key)
        if current["origin"] in (generic.ORIGIN_USER, generic.ORIGIN_OVERRIDE):
            # An earlier turn's explicit choice outranks a fresh build.
            protected.append(key)
            continue
        from_user = quoted_from_brief(claimed.get(key), brief)
        if seen and stated and stated != seen and (from_user or brief_supports(stated, brief)):
            result = generic.set_field(result, key, stated, generic.ORIGIN_OVERRIDE, observed=seen)
        elif seen:
            result = generic.set_field(result, key, seen, generic.ORIGIN_ASSET, observed=seen)
        elif from_user or brief_supports(value, brief):
            result = generic.set_field(result, key, value, generic.ORIGIN_USER)
        else:
            result = generic.set_field(result, key, value, generic.ORIGIN_INVENTED)
        changed.append(key)
    # Seen in the picture, so the compile may not quietly drop them; without
    # media the model is proposing an arrangement, which is not a fact.
    result, relation_count = _apply_relations(
        result, parsed.get("relations"),
        origin=generic.ORIGIN_ASSET if has_media else generic.ORIGIN_INVENTED,
    )
    if relation_count:
        changed.append("relations")
    return result, tuple(changed), tuple(protected)


def _people_map(raw: Any) -> dict[str, str]:
    """A list of people, flattened onto the document's per-person keys.

    The model is asked for [{subject, wardrobe, pose...}, {...}] rather than for
    "subject#2" keys, because a flat key list lets it fill each field
    independently -- observed on a two-dancer picture: it wrote the woman as A,
    then put the MAN's tights in A's wardrobe and left B's empty. An object per
    person makes the binding syntactic instead of a rule to remember.
    """
    if not isinstance(raw, list):
        return {}
    flattened: dict[str, str] = {}
    for index, person in enumerate(raw[: generic.MAX_PEOPLE], start=1):
        if not isinstance(person, dict):
            continue
        for field in generic.PERSON_FIELDS:
            value = person.get(field)
            if isinstance(value, str) and value.strip():
                flattened[generic.person_key(field, index)] = value
    return flattened


def _apply_relations(doc: dict[str, Any], raw: Any, *, origin: str) -> tuple[dict[str, Any], int]:
    """Fold a model's relation list into the document.

    A malformed edge is dropped, never fatal: relations are an improvement on
    the document and one bad row must not cost the user the whole build. The
    existing set is replaced rather than merged, because a rebuild re-reads the
    same picture -- except for edges the user owns, which survive.
    """
    if not isinstance(raw, list):
        return doc, 0
    kept = [edge for edge in generic.edges(doc) if edge.get("origin") in (generic.ORIGIN_USER, generic.ORIGIN_OVERRIDE)]
    added: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            edge = generic.validate_edge({**item, "origin": origin})
        except generic.GenericError:
            continue
        # A letter is only a person if the document describes that person. The
        # model answered a two-dancer picture with one blob subject and a
        # relation "A on B" -- letters pointing at nobody, which would read as
        # structure while meaning nothing.
        if any(
            (index := generic.endpoint_person(end)) and not generic.record(doc, generic.person_key("subject", index))["value"]
            for end in (edge["from"], edge["to"])
        ):
            continue
        added.append(edge)
    if not added and not kept:
        return doc, 0
    try:
        return generic.set_edges(doc, [*kept, *added][: generic.MAX_EDGES]), len(added)
    except generic.GenericError:
        return doc, 0


def _parsed_goals(raw: Any) -> list[dict[str, Any]]:
    """Accept the model's proposed goals, downgrading anything unverifiable.

    A "field" goal with no fields, or a "presence" goal with nothing to look
    for, cannot be checked deterministically -- so it becomes a judged goal
    rather than a check that silently always passes.
    """
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[: goal_ledger.MAX_GOALS]:
        if not isinstance(item, dict):
            continue
        text = normalize_unicode_text(str(item.get("text") or "")).strip()
        if not text:
            continue
        kind = item.get("kind") if item.get("kind") in goal_ledger.KINDS else goal_ledger.KIND_JUDGED
        fields = tuple(name for name in (item.get("fields") or []) if generic.is_field(name))
        must_include = tuple(
            normalize_unicode_text(str(value)).strip()
            for value in (item.get("must_include") or [])
            if str(value).strip()
        )
        if kind == goal_ledger.KIND_FIELD and not fields:
            kind = goal_ledger.KIND_JUDGED
        if kind == goal_ledger.KIND_PRESENCE and not must_include:
            kind = goal_ledger.KIND_JUDGED
        try:
            result.append(goal_ledger.new_goal(text, kind, fields=fields, must_include=must_include))
        except goal_ledger.GoalError:
            continue
    return result


STANDING_INSTRUCTION = re.compile(
    r"(?i)\b(?:always|never|from now on|going forward|make sure|ensure|each time|every time|"
    r"don'?t ever|keep (?:it|her|him|them|the)\b)"
)


def apply_turn(
    doc: dict[str, Any],
    goals: list[dict[str, Any]],
    text: str,
    *,
    has_media: bool,
    message: str = "",
) -> dict[str, Any]:
    """Fold one turn into the document and the ledger.

    Returns what changed so the conversation log can show it. A re-observation
    that touched nothing because the user owns those fields is reported, not
    swallowed: "I corrected it and nothing happened" must be visible.
    """
    parsed, _salvaged = _parse_json_object(
        text,
        "INVALID_GENERIC_TURN",
        "The model did not return a usable answer. Say it again, or rephrase.",
    )
    reply = normalize_unicode_text(str(parsed.get("reply") or "")).strip()[:MAX_MESSAGE_CHARS]
    patch = _clean_map(parsed.get("patch"))
    observed = _clean_map(parsed.get("observed")) if has_media else {}
    result = doc
    changed: list[str] = []
    protected: list[str] = []
    if observed:
        result, observed_changed, observed_protected = generic.apply_patch(
            result, observed, source=generic.SOURCE_OBSERVE
        )
        changed.extend(observed_changed)
        protected.extend(observed_protected)
    if patch:
        result, patch_changed, _protected = generic.apply_patch(result, patch, source=generic.SOURCE_USER)
        changed.extend(key for key in patch_changed if key not in changed)
    # The user correcting an arrangement owns it, exactly like a field they fix:
    # a later re-observation of the same picture must not walk it back.
    result, relation_count = _apply_relations(result, parsed.get("relations"), origin=generic.ORIGIN_USER)
    if relation_count and "relations" not in changed:
        changed.append("relations")
    additions = _parsed_goals(parsed.get("goals"))
    if not additions and STANDING_INSTRUCTION.search(message or ""):
        # The user said "always" / "from now on" and the model recorded no goal.
        # Their wording is the requirement; keeping it as a judged goal in their
        # own words is better than letting the instruction expire with the turn.
        try:
            additions = [goal_ledger.new_goal(message.strip())]
        except goal_ledger.GoalError:
            additions = []
    ledger, added = goal_ledger.merge(goals, additions)
    if not reply:
        reply = "Updated." if changed else "Nothing to change."
    return {
        "reply": reply,
        "generic": result,
        "goals": ledger,
        "changed": tuple(changed),
        "protected": tuple(protected),
        "goals_added": tuple(added),
    }
