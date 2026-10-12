"""Writing a dropped fact back in, instead of asking for the whole prompt again.

A compiled draft fails its audit in two different ways, and only one of them
needs the prompt rewritten:

    structural  a section is missing, a timestamp is out of range, a reference
                tag was invented, a standing goal is unmet. The text that IS
                there is wrong, so the model has to write it again.
    additive    a fact the user established never reached the prose. The text
                that is there is fine; a sentence is simply absent.

The repair loop treated both the same, and `prompt_repair.narrow_repair_messages`
ends with "Return the complete corrected prompt" -- so an additive failure was
fixed by re-emitting all 450 words, which is a fresh chance to drop a DIFFERENT
fact. Measured on a five-picture Reference compile: three corrections spent,
five established facts still missing, and the loop handed back the first draft.

Insertion cannot do that. The draft is never re-emitted. The model is asked only
for the missing sentences, each one is held to `generic.covers` -- the same bar
the audit uses -- and anything it will not write acceptably is built from the
document value directly. Everything outside the inserted sentences stays
byte-identical, so the one thing an insertion can never do is lose something.
"""
from __future__ import annotations

import re
from typing import Any

from . import generic, prompt_audit
from .scene_bible import proper_nouns


# Dialogue is the one locked field whose restoration is structural rather than
# additive: a spoken line needs <d>...</d> and a stable speaker ID, and
# `prompt_audit` fails a prompt carrying one without the other. Inserting it as
# plain prose would trade a dropped fact for a format failure, so it stays with
# the rewrite loop, which can put both halves in at once.
STRUCTURAL_FIELDS = frozenset({"dialogue"})

# Tags and timestamps are audited structure, not prose. A sentence carrying one
# can invent a reference, open a dialogue line, or push a cue past the end of
# the clip -- failures this pass is supposed to be too narrow to cause. Refusing
# the sentence is cheaper than auditing our way back out of it.
_TAG = re.compile(r"<[^>]*>")
_SUBJECT_TAG = re.compile(r"<Subject\s+\d+>", re.IGNORECASE)
_TIMESTAMP = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")

# One line per restored fact, so a 7B model is not also being asked to hold a
# JSON object open. Anything that is not `key | sentence` is dropped rather than
# guessed at; the literal fallback covers whatever the model failed to deliver.
_PATCH_LINE = re.compile(r"^\s*[-*•]?\s*([A-Za-z_]+(?:#\d+)?)\s*\|\s*(.+?)\s*$")

MAX_PATCH_TOKENS = 700

# How a restored fact is phrased when the model declined to phrase it. These are
# a backstop, not the normal path: correctness first, so each template states the
# fact plainly and leaves the value verbatim rather than trying to weave it in.
_PERSON_TEMPLATES = {
    "subject": "{who} is {value}.",
    "appearance": "{who} has {value}.",
    "wardrobe": "{who} is wearing {value}.",
    "action": "{who} is {value}.",
    "pose": "{who} is {value}.",
    "expression": "{who}'s expression is {value}.",
}
_SCENE_TEMPLATES = {
    "location": "The scene takes place in {value}.",
    "setting_detail": "The setting includes {value}.",
    "era": "The era is {value}.",
    "time_of_day": "The time of day is {value}.",
    "weather": "The weather is {value}.",
    "staging": "In frame, {value}.",
    "camera": "The shot is {value}.",
    "lighting": "The lighting is {value}.",
    "style": "The visual style is {value}.",
    "mood": "The mood is {value}.",
    "visible_text": "Visible text reads: {value}.",
    "soundscape": "The soundscape is {value}.",
    "music": "{value}.",
    # `dialogue` is in STRUCTURAL_FIELDS and `exclusions` is never auditable
    # (targets.base._auditable), so neither reaches a template.
}

# Audit lists an insertion must not lengthen. `severity` cannot see these -- it
# weighs dropped facts, unmet goals and the shared-failure count -- so a format
# regression caused by inserted text would otherwise look like an improvement.
_STRUCTURAL_COUNTS = (
    "missing_sections",
    "invalid_timestamps",
    "internal_video_representation_terms",
    "missing_reference_tags",
    "unexpected_reference_tags",
    "unsupported_camera_directions",
    "explicit_constraint_violations",
)


def additive_keys(audit: dict[str, Any], doc: dict[str, Any] | None) -> tuple[str, ...]:
    """The dropped locked facts that can be written back in as they stand.

    Scoped to the generic document on purpose. `targets.h3.audit` lets
    `shared_checks` overwrite `lock_violations`, so the key list is the
    document's whenever there is a document -- and the scene-bible keys it
    replaces address a different field set this cannot build a sentence from.
    """
    if not isinstance(doc, dict):
        return ()
    keys: list[str] = []
    for key in audit.get("lock_violations") or []:
        parsed = generic.split_key(key)
        if not parsed or parsed[0] in STRUCTURAL_FIELDS:
            continue
        if parsed[0] not in _PERSON_TEMPLATES and parsed[0] not in _SCENE_TEMPLATES:
            continue
        if generic.record(doc, key)["value"].strip():
            keys.append(key)
    return tuple(keys)


def _safe(sentence: str, draft: str) -> bool:
    """A sentence that can be spliced in without changing audited structure."""
    text = (sentence or "").strip()
    if not text:
        return False
    # A <Subject N> handle is allowed, but only one the draft already defines --
    # otherwise the insertion introduces a subject nothing in the prompt binds.
    for tag in _SUBJECT_TAG.findall(text):
        if tag not in draft:
            return False
    return not _TAG.search(_SUBJECT_TAG.sub("", text)) and not _TIMESTAMP.search(text)


def patch_messages(doc: dict[str, Any], draft: str, keys: tuple[str, ...]) -> list[dict[str, str]]:
    """Ask for the missing sentences only. The draft is context, never output."""
    wanted = "\n".join(
        f"{key} | {generic.label_for(key, doc)} | {generic.record(doc, key)['value']}"
        for key in keys
    )
    return [
        {
            "role": "system",
            "content": (
                "You are adding missing sentences to a finished prompt. Do NOT return the prompt. Do NOT "
                "rewrite, shorten, summarise or comment on it. For each fact listed below, write ONE "
                "sentence, in the same voice and tense as the draft, that states that fact and can be "
                "dropped into the draft as it stands. Use every distinctive word of the fact's value. "
                "Refer to a person only the way the draft already refers to them, and never by a personal "
                "name. Do not write a numbered media tag, a dialogue tag, or a timestamp. Answer with one "
                "line per fact in the form `key | sentence` and nothing else."
            ),
        },
        {
            "role": "user",
            "content": f"DRAFT (context only, do not return it):\n{draft}\n\nMISSING FACTS (key | field | value):\n{wanted}",
        },
    ]


def parse_patch(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """`key | sentence` lines, for the keys that were actually asked for."""
    allowed = set(keys)
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _PATCH_LINE.match(line)
        if not match:
            continue
        key, sentence = match.group(1), match.group(2).strip().strip("`").strip()
        if key in allowed and key not in found and sentence:
            found[key] = sentence
    return found


def _lead_lower(value: str) -> str:
    """"Standing, angled left" -> "standing, angled left", for mid-sentence use.

    A leading capital in the document is sentence case, not a name -- the same
    ambiguity `scene_bible.name_check` exists to resolve. It is left alone when
    the same word is capitalised again somewhere it is not sentence-initial,
    which is the evidence available without a dictionary. A genuine name that
    appears exactly once, at the front, is lowercased; that is cosmetic, it does
    not affect the audit (stems are case-folded), and this path only runs when
    the model declined to write the sentence itself.
    """
    head, separator, rest = (value or "").partition(" ")
    word = head.strip(",.;:")
    # An acronym stays as it is -- but the article "A" is one letter and all
    # upper case, so the length test is what keeps "A woman in her 30s" from
    # being mistaken for one.
    if not head[:1].isupper() or (len(word) > 1 and word.isupper()):
        return value
    if head.strip(",.;:").lower() in proper_nouns(rest):
        return value
    return head[:1].lower() + head[1:] + separator + rest


def literal_sentence(doc: dict[str, Any], key: str, draft: str) -> str:
    """The fact stated plainly, built from the document rather than the model."""
    field, index = generic.split_key(key)
    value = generic.record(doc, key)["value"].strip().rstrip(".")
    if not value:
        return ""
    if field in _SCENE_TEMPLATES:
        return _SCENE_TEMPLATES[field].format(value=_lead_lower(value))
    # The subject field describes the person, so describing them again to
    # introduce it says the same thing twice ("The woman in her 30s is a woman
    # in her 30s"). It gets the bare handle.
    who = _who(doc, index, draft, describe=field != "subject")
    return _PERSON_TEMPLATES[field].format(who=who, value=_lead_lower(value))


_ARTICLE = re.compile(r"(?i)^(?:a|an|the)\s+")


def _who(doc: dict[str, Any], index: int, draft: str, *, describe: bool = True) -> str:
    """How the draft already refers to this person.

    Never the name the user gave them: a name in a finished prompt drags the
    render toward whoever the video model thinks it belongs to, which is why
    `conversation` resolves names back into descriptions at compile time. The
    <Subject N> handle is used only where the draft has already defined it --
    non-Reference H3 prompts and both image targets have no such handle, and
    `sequence_output` actively rejects one.
    """
    tag = f"<Subject {index}>"
    if tag in draft:
        return tag
    subject = generic.record(doc, generic.person_key("subject", index))["value"].strip().rstrip(".")
    if describe and subject:
        return f"The {_lead_lower(_ARTICLE.sub('', subject))}"
    return "The subject" if index == 1 else f"Subject {generic.person_letter(index)}"


def resolve(
    doc: dict[str, Any],
    draft: str,
    keys: tuple[str, ...],
    written: dict[str, str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """(additions to splice in, keys nothing acceptable could be written for).

    The model's sentence is preferred -- it is in the draft's own voice -- but
    only if it is safe to splice AND actually carries the fact. A sentence that
    paraphrases the value away would be accepted here and then fail the very
    audit this pass exists to clear, so it is measured with `generic.covers`
    before it is trusted.
    """
    additions: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for key in keys:
        value = generic.record(doc, key)["value"]
        candidate = written.get(key, "")
        if candidate and _safe(candidate, draft) and generic.covers(value, candidate):
            additions.append((key, candidate))
            continue
        literal = literal_sentence(doc, key, draft)
        if literal and _safe(literal, draft) and generic.covers(value, literal):
            additions.append((key, literal))
            continue
        unresolved.append(key)
    return additions, unresolved


def accept(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str | None]:
    """Keep an insertion only when it strictly improved the audit.

    Adding text cannot remove a fact, but it can still make the AUDIT worse --
    inserted prose counts toward a section's word budget and is scanned by every
    structural check. A draft that trades two restored facts for an invented
    reference tag is not a better draft, so the whole insertion is dropped
    rather than partially kept: the pre-insertion draft is still intact.
    """
    recovered = len(before.get("lock_violations") or []) - len(after.get("lock_violations") or [])
    if recovered <= 0:
        return False, "no established fact was recovered"
    for key in _STRUCTURAL_COUNTS:
        if len(after.get(key) or []) > len(before.get(key) or []):
            return False, f"the inserted text introduced {key.replace('_', ' ')}"
    if prompt_audit.severity(after) >= prompt_audit.severity(before):
        return False, "the audit did not improve"
    return True, None


def insert_into_section(prompt: str, sentence: str, section: str, sections: tuple[str, ...]) -> str:
    """Put `sentence` at the end of `section`'s body, leaving the rest untouched.

    Appended rather than woven in: an insertion that rewrites nothing cannot
    drop anything, which is the entire point of this pass. A missing header is
    the normal case, not an error -- only H3's Reference mode emits the six
    official sections, and every other prompt here is a single block.
    """
    found: dict[str, re.Match[str]] = {}
    for name in sections:
        match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*", prompt)
        if match:
            found[name] = match
    anchor = found.get(section)
    if anchor is None:
        return append_sentence(prompt, sentence)
    later = [match.start() for match in found.values() if match.start() > anchor.start()]
    end = min(later) if later else len(prompt)
    body = prompt[anchor.end():end]
    cut = end - (len(body) - len(body.rstrip()))
    return prompt[:cut] + ("" if not body.strip() else " ") + sentence + prompt[cut:]


def append_sentence(prompt: str, sentence: str) -> str:
    """The whole-prompt fallback: one more sentence on the end of the prose."""
    return f"{prompt.rstrip()} {sentence}" if prompt.strip() else sentence
