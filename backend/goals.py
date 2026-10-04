"""The goal ledger: standing instructions, re-applied and re-checked every time.

An instruction is not a patch. "Make sure the prompt follows the clothing
colours in the image" is a requirement that must still hold after the next
regeneration and after switching to a different model -- so a turn that patches
one field and forgets the rule has not done what was asked.

Every goal is therefore (1) injected into every generation and every compile,
(2) verified against the result, and (3) repaired when unmet. A goal that cannot
be met is reported unmet; it is never quietly dropped and never reported green.

Three verification kinds, because honesty about what a machine can check matters
more than pretending one mechanism covers arbitrary English:

    field     the goal implies specific generic fields must be established from
              the reference (not left unspecified or invented) -- deterministic
    presence  a named fact must appear in the compiled prompt -- deterministic
    judged    everything else -- one verifier pass, PASS/FAIL with a reason
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import uuid4

from . import generic
from .text_normalization import normalize_unicode_text


KIND_FIELD = "field"
KIND_PRESENCE = "presence"
KIND_JUDGED = "judged"
KINDS = (KIND_FIELD, KIND_PRESENCE, KIND_JUDGED)

VERDICT_MET = "met"
VERDICT_UNMET = "unmet"
VERDICT_PENDING = "pending"
VERDICTS = (VERDICT_MET, VERDICT_UNMET, VERDICT_PENDING)

MAX_GOALS = 24
MAX_GOAL_CHARS = 400
# A presence goal names a handful of things that must appear, not a payload. Both
# the route and the model can supply these, and an uncapped list is a way to
# write gigabytes into the session file one request at a time.
MAX_GOAL_INCLUDES = 8
MAX_GOAL_FIELDS = len(generic.FIELDS) * generic.MAX_PEOPLE


class GoalError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_goal(
    text: str,
    kind: str = KIND_JUDGED,
    *,
    fields: tuple[str, ...] = (),
    must_include: tuple[str, ...] = (),
) -> dict[str, Any]:
    value = normalize_unicode_text(text or "").strip()
    if not value:
        raise GoalError("EMPTY_GOAL", "A goal needs text.")
    if len(value) > MAX_GOAL_CHARS:
        raise GoalError("GOAL_TOO_LONG", f"A goal cannot exceed {MAX_GOAL_CHARS} characters.")
    if kind not in KINDS:
        raise GoalError("INVALID_GOAL_KIND", f"Unknown goal kind: {kind}")
    unknown = [name for name in fields if not generic.is_field(name)]
    if unknown:
        raise GoalError("UNKNOWN_FIELD", f"Unknown generic field(s) in goal: {sorted(unknown)}")
    if len(fields) > MAX_GOAL_FIELDS:
        raise GoalError("INVALID_GOAL", "A goal cannot name more fields than the document has.")
    includes = [normalize_unicode_text(str(item)).strip() for item in must_include]
    includes = [item for item in includes if item]
    if len(includes) > MAX_GOAL_INCLUDES:
        raise GoalError("TOO_MANY_INCLUDES", f"A goal cannot require more than {MAX_GOAL_INCLUDES} things to appear.")
    if any(len(item) > MAX_GOAL_CHARS for item in includes):
        raise GoalError("GOAL_TOO_LONG", f"Each required phrase must be under {MAX_GOAL_CHARS} characters.")
    if kind == KIND_FIELD and not fields:
        raise GoalError("INVALID_GOAL", "A field goal must name at least one generic field.")
    if kind == KIND_PRESENCE and not must_include:
        raise GoalError("INVALID_GOAL", "A presence goal must name what has to appear.")
    return {
        "id": uuid4().hex[:12],
        "text": value,
        "kind": kind,
        "fields": list(dict.fromkeys(fields)),
        "must_include": includes,
        "enabled": True,
        "verdict": VERDICT_PENDING,
        "reason": "",
        "created_at": time.time(),
    }


def validate(goals: Any) -> list[dict[str, Any]]:
    if goals is None:
        return []
    if not isinstance(goals, list):
        raise GoalError("INVALID_GOALS", "Goals must be a list.")
    if len(goals) > MAX_GOALS:
        raise GoalError("TOO_MANY_GOALS", f"A session cannot hold more than {MAX_GOALS} goals.")
    result = []
    for item in goals:
        if not isinstance(item, dict):
            raise GoalError("INVALID_GOALS", "Each goal must be an object.")
        goal = new_goal(
            item.get("text", ""),
            item.get("kind", KIND_JUDGED),
            fields=tuple(item.get("fields") or ()),
            must_include=tuple(item.get("must_include") or ()),
        )
        goal["id"] = str(item.get("id") or goal["id"])[:32]
        goal["enabled"] = bool(item.get("enabled", True))
        goal["verdict"] = item.get("verdict") if item.get("verdict") in VERDICTS else VERDICT_PENDING
        goal["reason"] = str(item.get("reason") or "")[:400]
        goal["created_at"] = item.get("created_at") or goal["created_at"]
        result.append(goal)
    return result


def active(goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [goal for goal in goals or [] if goal.get("enabled", True)]


def merge(goals: list[dict[str, Any]], additions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Add goals, skipping ones that repeat an existing goal's wording."""
    existing = {goal["text"].strip().lower() for goal in goals}
    result = list(goals)
    added: list[str] = []
    for goal in additions:
        if goal["text"].strip().lower() in existing:
            continue
        if len(result) >= MAX_GOALS:
            break
        result.append(goal)
        existing.add(goal["text"].strip().lower())
        added.append(goal["text"])
    return result, added


def toggle(goals: list[dict[str, Any]], goal_id: str, enabled: bool) -> list[dict[str, Any]]:
    found = False
    result = []
    for goal in goals:
        if goal["id"] == goal_id:
            goal = {**goal, "enabled": bool(enabled)}
            found = True
        result.append(goal)
    if not found:
        raise GoalError("GOAL_NOT_FOUND", "That goal is no longer in the ledger.")
    return result


def remove(goals: list[dict[str, Any]], goal_id: str) -> list[dict[str, Any]]:
    result = [goal for goal in goals if goal["id"] != goal_id]
    if len(result) == len(goals):
        raise GoalError("GOAL_NOT_FOUND", "That goal is no longer in the ledger.")
    return result


def render(goals: list[dict[str, Any]]) -> str:
    """The standing-goals block injected into every generation and compile."""
    lines = [f"- {goal['text']}" for goal in active(goals)]
    return "\n".join(lines)


EDGE_GOAL_PREFIX = "relation:"
EXCLUSION_GOAL_PREFIX = "exclusions:"


def is_derived(goal: dict[str, Any]) -> bool:
    """Whether this goal was derived from the document for one compile."""
    identifier = str(goal.get("id") or "")
    return identifier == "exclusions" or identifier.startswith("edge:")


def without_derived(goals: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The user's own standing goals, with the per-compile ones stripped.

    Derived goals are rebuilt from the document on every compile. Storing one
    would outlive the relation it came from -- and, observed live, the compile
    wrote its evaluated list straight back to the session, so every compile
    added another copy of every relation to the user's ledger.
    """
    return [goal for goal in (goals or []) if not is_derived(goal)]


def from_exclusions(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What must not appear, as a question a verifier can actually answer.

    A negative cannot be audited by looking for words: the prose that obeys
    "no visible props" never contains the phrase, and the prose that breaks it
    ("mirrors and barres along the walls") does not contain it either.
    """
    if not isinstance(doc, dict):
        return []
    record = generic.record(doc, "exclusions")
    if not record["value"] or record["origin"] not in generic.LOCKED_ORIGINS:
        return []
    text = f"{EXCLUSION_GOAL_PREFIX} the prompt must not include: {record['value']}"
    try:
        goal = new_goal(text[:MAX_GOAL_CHARS], KIND_JUDGED)
    except GoalError:
        return []
    goal["id"] = "exclusions"
    return [goal]


def from_edges(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Locked relations, as goals the verifier already knows how to check.

    A relation cannot be audited by counting words: "B on A (across her lap)"
    shares every token with "A on B", and three words of overlap prove nothing
    either way. But "does this prompt say B is lying across A's lap?" is exactly
    the yes/no the judged-goal verifier was built for -- so a locked edge
    becomes an ephemeral judged goal for the duration of one compile, and the
    repair machinery treats a dropped relation like any other unmet goal.

    Ephemeral on purpose: these are derived from the document, so they must not
    be stored beside the user's own standing goals or they would outlive the
    relation they came from.
    """
    if not isinstance(doc, dict):
        return []
    result: list[dict[str, Any]] = []
    for edge in generic.locked_edges(doc):
        subject, target = _named(doc, edge["from"]), _named(doc, edge["to"])
        meaning = generic.RELATIONS[edge["rel"]]
        qualifier = f" ({edge['qualifier']})" if edge.get("qualifier") else ""
        text = f"{EDGE_GOAL_PREFIX} {subject} is {meaning} {target}{qualifier}"
        try:
            goal = new_goal(text[:MAX_GOAL_CHARS], KIND_JUDGED)
        except GoalError:
            continue
        goal["id"] = f"edge:{edge['from']}:{edge['rel']}:{edge['to']}"
        result.append(goal)
    return result


def _named(doc: dict[str, Any], endpoint: str) -> str:
    """A person letter reads as their description; a noun is already one."""
    index = generic.endpoint_person(endpoint)
    if not index:
        return endpoint
    value = generic.record(doc, generic.person_key("subject", index))["value"]
    who = generic.names(doc).get(index) or f"person {endpoint}"
    return f"{value} ({who})" if value else who


def _field_verdict(goal: dict[str, Any], doc: dict[str, Any]) -> tuple[bool, str]:
    unmet: list[str] = []
    notes: list[str] = []
    for key in goal["fields"]:
        entry = generic.record(doc, key)
        label = generic.label_for(key)
        if entry["origin"] == generic.ORIGIN_UNSPECIFIED:
            unmet.append(f"{label} is still unspecified")
        elif entry["origin"] == generic.ORIGIN_INVENTED:
            unmet.append(f"{label} was invented rather than taken from the reference")
        elif entry["origin"] == generic.ORIGIN_OVERRIDE:
            notes.append(f"{label} is your override")
        elif entry["origin"] == generic.ORIGIN_ASSET:
            notes.append(f"{label} from the reference")
        else:
            notes.append(f"{label} in your words")
    if unmet:
        return False, "; ".join(unmet)
    return True, ", ".join(notes)


PRESENCE_THRESHOLD = 0.6
"""Share of a phrase's distinctive words the prompt must carry.

The same bar the locked-fact audit uses, for the same reason: the compile is
expected to reword, and only vocabulary *going missing* is evidence the fact
went with it.
"""

def _phrase_present(phrase: str, prompt: str, prompt_stems: set[str]) -> bool:
    """Whether the compiled prompt still carries this required phrase.

    Deliberately NOT a substring test any more. `must_include` holds a sentence
    the conversation wrote ("standing in front of a wooden desk"), and the
    compile is *supposed* to reword it -- "stands directly in front of a
    polished wooden desk" satisfies the user and failed the old check, which
    then burned a repair pass and reported the goal unmet. An inserted adverb
    was enough to do it.

    A literal hit still wins, so "always say Coca-Cola" behaves exactly as
    before. Otherwise every name must survive (a name is the handle the user
    steers with, never averaged away) and most of the distinctive words with it.

    Known limit: this is bag-of-words, so it cannot see negation -- "no
    undergarments" and "undergarments" carry the same tokens. A goal whose
    meaning turns on a negation or on nuance belongs to the `judged` kind,
    which spends a verifier pass on it.
    """
    normalized = normalize_unicode_text(phrase or "").strip().lower()
    if not normalized:
        return True
    if normalized in (prompt or "").lower():
        return True
    wanted = generic.distinctive_stems(phrase)
    if not wanted:
        return False
    # The same name rule the locked-fact audit uses: a name must survive whole,
    # but a capital that is only sentence case is an ordinary word.
    _considered, dropped = generic.name_check(phrase, prompt or "")
    if dropped:
        return False
    return len(wanted & prompt_stems) / len(wanted) >= PRESENCE_THRESHOLD


def _presence_verdict(goal: dict[str, Any], prompt: str) -> tuple[bool, str]:
    prompt_stems = generic.distinctive_stems(prompt or "")
    missing = [item for item in goal["must_include"] if not _phrase_present(item, prompt, prompt_stems)]
    if missing:
        return False, "missing from the prompt: " + ", ".join(missing)
    return True, "present in the prompt"


def evaluate(
    goals: list[dict[str, Any]],
    doc: dict[str, Any] | None,
    prompt: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Check every enabled goal we can check without a model.

    Returns (goals, violations, pending). `pending` holds the judged goals that
    still need a verifier pass -- they are NOT counted as met in the meantime.
    """
    result: list[dict[str, Any]] = []
    violations: list[str] = []
    pending: list[dict[str, Any]] = []
    for goal in goals or []:
        if not goal.get("enabled", True):
            result.append({**goal, "verdict": VERDICT_PENDING, "reason": "paused"})
            continue
        if goal["kind"] == KIND_FIELD and doc is not None:
            met, reason = _field_verdict(goal, doc)
        elif goal["kind"] == KIND_PRESENCE:
            met, reason = _presence_verdict(goal, prompt)
        else:
            updated = {**goal, "verdict": VERDICT_PENDING, "reason": "awaiting verification"}
            result.append(updated)
            pending.append(updated)
            continue
        result.append({**goal, "verdict": VERDICT_MET if met else VERDICT_UNMET, "reason": reason})
        if not met:
            violations.append(f"unmet goal - {goal['text']} ({reason})")
    return result, violations, pending


VERIFY_INSTRUCTIONS = (
    "You are verifying a finished image or video prompt against the user's standing goals. "
    "For each goal decide whether the prompt satisfies it. A goal is satisfied when the prompt's own "
    "text carries what the goal asks for; do not reward intent or near misses, and do not penalise "
    "wording that differs while the substance is present. "
    'Return only JSON: {"verdicts":[{"id":"<goal id>","met":true,"reason":"<one short clause>"}]} '
    "with one entry per goal, no commentary and no Markdown."
)


def verify_messages(pending: list[dict[str, Any]], prompt: str) -> list[dict[str, str]]:
    payload = {
        "goals": [{"id": goal["id"], "goal": goal["text"]} for goal in pending],
        "prompt": prompt,
    }
    return [
        {"role": "system", "content": VERIFY_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_verification(text: str, pending: list[dict[str, Any]]) -> dict[str, tuple[bool, str]]:
    """Read the verifier's answer. An unreadable answer leaves goals pending.

    Deliberately forgiving of packaging and unforgiving of omission: a goal the
    verifier skipped stays pending rather than defaulting to met.
    """
    value = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", value)
    if fence:
        value = fence.group(1)
    match = re.search(r"\{[\s\S]*\}", value)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(verdicts, list):
        return {}
    wanted = {goal["id"] for goal in pending}
    result: dict[str, tuple[bool, str]] = {}
    for item in verdicts:
        if not isinstance(item, dict):
            continue
        goal_id = str(item.get("id") or "")
        if goal_id not in wanted or not isinstance(item.get("met"), bool):
            continue
        result[goal_id] = (item["met"], str(item.get("reason") or "")[:200])
    return result


def apply_verdicts(
    goals: list[dict[str, Any]],
    verdicts: dict[str, tuple[bool, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    violations: list[str] = []
    for goal in goals:
        verdict = verdicts.get(goal["id"])
        if verdict is None:
            result.append(goal)
            continue
        met, reason = verdict
        result.append({**goal, "verdict": VERDICT_MET if met else VERDICT_UNMET, "reason": reason})
        if not met:
            violations.append(f"unmet goal - {goal['text']} ({reason})" if reason else f"unmet goal - {goal['text']}")
    return result, violations


def unmet(goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [goal for goal in goals or [] if goal.get("enabled", True) and goal.get("verdict") == VERDICT_UNMET]
