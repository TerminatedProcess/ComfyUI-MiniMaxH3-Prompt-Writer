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

from . import generic, goals as goal_ledger
from .scene_bible import distinctive_tokens
from .text_normalization import normalize_unicode_text

FIELD_LIST = ", ".join(generic.FIELDS)

BUILD_INSTRUCTIONS = f"""You are building a model-agnostic scene document for an image or video prompt. Return only JSON.

Use exactly this shape:
{{"observed": {{"<field>": "<what the reference media actually shows>"}}, "scene": {{"<field>": "<the fact for the finished shot>"}}, "from_brief": {{"<field>": "<the exact words copied from the user's brief that this field came from>"}}}}

Fields, all optional in all three objects: {FIELD_LIST}.

Rules:
- "observed" describes ONLY what is literally visible or audible in the supplied reference media. Omit a field you cannot see. Never guess there. If no media is supplied, return an empty object.
- "scene" is the shot to make: the user's brief first, the reference media second, your own judgement last.
- ONE short factual clause per field: a phrase or a single sentence, at most 25 words. Not a paragraph. No lists, no hedging, no "maybe".
- Colours, materials, counts and spatial relationships belong in the field they describe; be specific about them.
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
NO_AUDIO_BUILD_CLAUSE = (
    "The clip is silent or will be scored later: leave dialogue, soundscape and music out of the document "
    "entirely unless the brief explicitly asks for a sound, a line of dialogue or music."
)
NSFW_BUILD_CLAUSE = (
    "Adult or explicit content is permitted where the brief calls for it; describe it plainly, without euphemism "
    "or refusal."
)

TURN_INSTRUCTIONS = f"""You are steering a model-agnostic scene document through conversation. Return only JSON.

Use exactly this shape:
{{"reply": "<one short sentence to the user>", "patch": {{"<field>": "<new value>"}}, "observed": {{"<field>": "<what the reference media actually shows>"}}, "goals": [{{"text": "<standing instruction>", "kind": "field|presence|judged", "fields": ["<field>"], "must_include": ["<exact text>"]}}]}}

Fields: {FIELD_LIST}.

Rules:
- "patch" carries facts the user just stated or changed. Only fields they actually addressed. Their words win over the reference media.
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
        if key not in generic.FIELDS or not isinstance(value, str):
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


def build_instructions(*, nsfw: bool, story: bool, no_audio: bool = False) -> str:
    parts = [BUILD_INSTRUCTIONS, STORY_BUILD_CLAUSE if story else FAITHFUL_BUILD_CLAUSE]
    if no_audio:
        parts.append(NO_AUDIO_BUILD_CLAUSE)
    if nsfw:
        parts.append(NSFW_BUILD_CLAUSE)
    return "\n\n".join(parts)


def turn_instructions(*, nsfw: bool, story: bool, no_audio: bool = False) -> str:
    parts = [TURN_INSTRUCTIONS]
    if no_audio:
        parts.append(NO_AUDIO_BUILD_CLAUSE)
    if story:
        parts.append(
            "Story builder is ON, so you may also fill fields the user has not addressed when the change implies "
            "them -- but never overwrite a fact they gave."
        )
    if nsfw:
        parts.append(NSFW_BUILD_CLAUSE)
    return "\n\n".join(parts)


def assemble_build(
    *,
    session_id: str,
    brief: str,
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    doc: dict[str, Any] | None,
    goals: list[dict[str, Any]],
    nsfw: bool,
    story: bool,
    duration_seconds: float | None,
    aspect_ratio: str | None,
    no_audio: bool = False,
) -> dict[str, Any]:
    """One request that reads the references and writes the whole document."""
    instructions = build_instructions(nsfw=nsfw, story=story, no_audio=no_audio)
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
            "nsfw": nsfw,
            "story": story,
            "no_audio": no_audio,
        },
        "media_inputs": media_inputs,
        "supporting_guides": [],
        "system_prompt": {"custom": False, "content": instructions},
        "messages": [
            {"role": "system", "name": "generic_prompt_contract", "content": instructions},
            {"role": "user", "content": user_content},
        ],
    }


def assemble_turn(
    *,
    session_id: str,
    message: str,
    doc: dict[str, Any],
    goals: list[dict[str, Any]],
    conversation: list[dict[str, Any]],
    manifest: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    nsfw: bool,
    story: bool,
    no_audio: bool = False,
) -> dict[str, Any]:
    """One conversation turn.

    The media is re-attached every turn when any is loaded: "you are not
    following the colours in the image" cannot be answered from the document
    alone, and asking the user to say which kind of correction they meant would
    put the mechanism in their way.
    """
    instructions = turn_instructions(nsfw=nsfw, story=story, no_audio=no_audio)
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
            "nsfw": nsfw,
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
    observed = _clean_map(parsed.get("observed")) if has_media else {}
    scene = _clean_map(parsed.get("scene"))
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
    for key in generic.FIELDS:
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
    return result, tuple(changed), tuple(protected)


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
        fields = tuple(name for name in (item.get("fields") or []) if name in generic.FIELDS)
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
