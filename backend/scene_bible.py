"""Scene bible: structured conversation state. No host, provider, or graph dependencies.

Conversation state is deliberately NOT the generated prose. Calibration showed
that asking a model to edit an existing brief makes it anchor on that text and
perform surface substitutions -- told to restyle a room to the 1970s it renamed
the era and kept the VHS tapes, across two prompt revisions and five explicit
seeds. Regenerating from scratch fixes the thoroughness but silently loses the
subject. Holding a small structured bible and re-rendering prose from it each
turn gets both: nothing anchors on stale text, and nothing drifts because every
established fact is restated as a constraint.

See docs/PLAN_IMAGINATION_AND_ASSETS.md for the measurements behind this.
"""
from __future__ import annotations

import re
from typing import Any

# Ordered: this is the order facts are presented to the expander, and reads as a
# natural description (who, doing what, where, when, shot how).
FIELDS = ("subject", "action", "location", "era", "time_of_day", "weather", "camera")

# Facts sourced from a reference image may never be silently altered. An explicit
# user instruction can still override one -- that is the user exercising tier 2
# over tier 1 -- but drift is a bug.
ORIGIN_USER = "user"
ORIGIN_ASSET = "asset"
ORIGIN_INVENTED = "invented"
ORIGINS = (ORIGIN_USER, ORIGIN_ASSET, ORIGIN_INVENTED)

_WORD = re.compile(r"[a-z0-9']+")
# Capitalised word inside a field value -- treated as a proper noun (see _proper_nouns).
_PROPER = re.compile(r"\b[A-Z][a-zA-Z']{1,}\b")
# "Two women:" opens a sentence, so "Two" is capitalised -- and a name that must
# survive whole is exactly what a count is not. Counts live or die on the ratio
# like any other word.
_NUMBER_WORDS = frozenset(
    "one two three four five six seven eight nine ten eleven twelve dozen "
    "first second third fourth fifth both".split()
)
# Tokens too common to prove a fact survived; matching on them yields false passes.
_STOPWORDS = frozenset(
    "a an and the his her its their of in on at to is are was were be been with "
    "from for by as it he she they them this that these those into over under "
    "man woman person people someone something".split()
)


class SceneBibleError(ValueError):
    """Raised with a stable uppercase code the frontend can key off."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_bible() -> dict[str, Any]:
    """An empty bible. Fields are filled by the intent resolver, not guessed here."""
    return {"fields": {}, "origins": {}}


def validate(bible: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bible, dict):
        raise SceneBibleError("INVALID_BIBLE", "The scene bible must be an object.")
    fields = bible.get("fields")
    origins = bible.get("origins")
    if not isinstance(fields, dict) or not isinstance(origins, dict):
        raise SceneBibleError("INVALID_BIBLE", "The scene bible is malformed.")
    for key, value in fields.items():
        if key not in FIELDS:
            raise SceneBibleError("UNKNOWN_FIELD", f"Unknown scene field: {key}")
        if not isinstance(value, str) or not value.strip():
            raise SceneBibleError("EMPTY_FIELD", f"Scene field '{key}' is empty.")
        if len(value) > 400:
            raise SceneBibleError("FIELD_TOO_LONG", f"Scene field '{key}' exceeds 400 characters.")
    for key, origin in origins.items():
        if key not in fields:
            raise SceneBibleError("ORPHAN_ORIGIN", f"Origin recorded for absent field: {key}")
        if origin not in ORIGINS:
            raise SceneBibleError("INVALID_ORIGIN", f"Unknown origin '{origin}' for field '{key}'.")
    return bible


def set_field(bible: dict[str, Any], key: str, value: str, origin: str) -> dict[str, Any]:
    if key not in FIELDS:
        raise SceneBibleError("UNKNOWN_FIELD", f"Unknown scene field: {key}")
    if origin not in ORIGINS:
        raise SceneBibleError("INVALID_ORIGIN", f"Unknown origin: {origin}")
    updated = {"fields": dict(bible["fields"]), "origins": dict(bible["origins"])}
    updated["fields"][key] = value.strip()
    updated["origins"][key] = origin
    return validate(updated)


def locked_fields(bible: dict[str, Any]) -> tuple[str, ...]:
    """Asset-derived fields, in FIELDS order."""
    return tuple(k for k in FIELDS if bible["origins"].get(k) == ORIGIN_ASSET)


def apply_update(
    bible: dict[str, Any],
    update: dict[str, str],
    *,
    explicit_targets: tuple[str, ...] = (),
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Merge a field update.

    Returns (bible, changed, overridden). A change to an asset-derived field is
    refused unless the field is named in `explicit_targets` -- i.e. the user
    asked for it by name. Refusing silently would strand the user; refusing
    loudly is why `overridden` is reported back to the UI.
    """
    if not isinstance(update, dict):
        raise SceneBibleError("INVALID_UPDATE", "The field update must be an object.")
    changed: list[str] = []
    overridden: list[str] = []
    result = bible
    locked = set(locked_fields(bible))
    for key in FIELDS:  # deterministic order regardless of dict ordering
        if key not in update:
            continue
        value = update[key]
        if not isinstance(value, str) or not value.strip():
            raise SceneBibleError("EMPTY_FIELD", f"Scene field '{key}' is empty.")
        if value.strip() == bible["fields"].get(key):
            continue
        if key in locked:
            if key not in explicit_targets:
                # Silent drift away from an asset is the failure mode locks exist
                # to prevent. Skip rather than raise: one protected field must not
                # abort an otherwise valid multi-field edit.
                continue
            overridden.append(key)
        origin = ORIGIN_USER if key in explicit_targets else bible["origins"].get(key, ORIGIN_INVENTED)
        result = set_field(result, key, value, origin)
        changed.append(key)
    return result, tuple(changed), tuple(overridden)


def render_constraints(bible: dict[str, Any]) -> str:
    """The ESTABLISHED FACTS block handed to the expander.

    Every field is restated every turn. That repetition is the mechanism: it is
    what stops regeneration from drifting the subject.
    """
    lines = []
    for key in FIELDS:
        value = bible["fields"].get(key)
        if not value:
            continue
        marker = " [from the user's reference image - reproduce exactly]" if \
            bible["origins"].get(key) == ORIGIN_ASSET else ""
        lines.append(f"- {key}: {value}{marker}")
    return "\n".join(lines)


def _strip_possessive(word: str) -> str:
    """"person 1's" must not carry a token no prose can ever contain."""
    for suffix in ("'s", "s'", "'"):
        if word.endswith(suffix) and len(word) > len(suffix):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> set[str]:
    words = (_strip_possessive(w) for w in _WORD.findall(text.lower()))
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _proper_nouns(text: str) -> set[str]:
    """Capitalised names inside a field value.

    A name is not just one more descriptive token: it is the handle the user
    steers with ("put Bob in a suit"). Integration testing caught the expander
    rendering a locked subject as "an adult male in his late 30s" -- every
    visual fact intact, the name gone -- which passed a ratio check because the
    name was one token in nine. Names are therefore all-or-nothing, never
    averaged away.
    """
    return {
        w.lower() for w in _PROPER.findall(text)
        if w.lower() not in _STOPWORDS and w.lower() not in _NUMBER_WORDS
    }


# A capitalised word that opens the text or a sentence inside it. Sentence case
# capitalises ordinary words there, so it is not proof of a name (see name_check).
_SENTENCE_START = re.compile(r"(?:^|[.!?]\s+|\n\s*)([A-Z][a-zA-Z']+)")

_STEM_SUFFIXES = ("ing", "ed", "es", "s")


def stem_token(word: str) -> str:
    """Crude English suffix strip, so a tense change is not a missing word.

    Not linguistics: both sides go through it, so "tilting"/"tilts" and
    "extended"/"extends" only have to agree with each other. Without it a
    compile that faithfully restated a locked fact in the present tense read as
    having dropped it -- an observed Action scored 0.58 against a 0.6 bar purely
    on `taking/take`, `extended/extends`, `tilting/tilts`, `holding/hold`, and
    bought a repair turn that corrected nothing.
    """
    # "lying" strips to nothing usable, while "lies" strips to "lie". The whole
    # -ying family (lying/dying/tying) needs the e back or the two never meet.
    if word.endswith("ying") and len(word) > 4:
        word = word[:-4] + "ie"
    else:
      for suffix in _STEM_SUFFIXES:
          # "dress" is not a plural: -ss never loses its s, or it stops agreeing
          # with "dresses".
          if suffix == "s" and word.endswith("ss"):
              continue
          if word.endswith(suffix) and len(word) - len(suffix) >= 3:
              word = word[: -len(suffix)]
              # "sitting" -> "sitt" -> "sit", to agree with "sits". English
              # doubles the consonant before -ing and -ed; nothing else in this
              # vocabulary ends in a doubled consonant that matters.
              if len(word) >= 4 and word[-1] == word[-2] and word[-1] not in "ls":
                  word = word[:-1]
              break
    if word.endswith("s") and not word.endswith("ss") and len(word) >= 4:
        word = word[:-1]
    # The -e verbs are why this is here at all: "taking" strips to "tak" while
    # "take" strips to nothing, so the two forms only meet if the final e goes
    # too. That single pair was enough to fail a locked Action twice over.
    if word.endswith("e") and len(word) >= 4:
        word = word[:-1]
    return word


# Public aliases: the generic prompt document reuses this token logic over a
# different field set, and importing a private name across modules hides that.
def distinctive_tokens(text: str) -> set[str]:
    return _tokens(text)


def distinctive_stems(text: str) -> set[str]:
    return {stem_token(token) for token in _tokens(text)}


def proper_nouns(text: str) -> set[str]:
    return _proper_nouns(text)


def name_check(value: str, prose: str) -> tuple[set[str], set[str]]:
    """(names this value is judged on, the ones the prose dropped).

    Capitalisation means "name" everywhere except at the start of a sentence,
    where it means nothing -- and the generic document is written in sentence
    case: "Taking a selfie with her arm extended toward the camera". "Taking"
    was read as a name, names are all-or-nothing, and a compile that said "to
    take a selfie" therefore failed the whole field outright no matter how
    faithful the rest of it was. Observed on a real compile, twice in a row,
    including on the repair.

    So a sentence-initial capital only counts as a name when the prose does not
    carry it as an ordinary word either -- which is exactly the case that
    matters: a dropped "Bob" is absent in every form, while a dropped "Taking"
    is right there as "take". A name found anywhere in the prose, capitalised or
    not, is satisfied.
    """
    names = _proper_nouns(value)
    if not names:
        return set(), set()
    prose_tokens = _tokens(prose)
    prose_stems = {stem_token(token) for token in prose_tokens}
    prose_names = _proper_nouns(prose)
    sentence_initial = {match.group(1).lower() for match in _SENTENCE_START.finditer(value)}
    considered: set[str] = set()
    missing: set[str] = set()
    for name in names:
        if name in sentence_initial and name not in prose_names and stem_token(name) in prose_stems:
            continue  # sentence case, not a name: the ratio check owns this word
        considered.add(name)
        if name not in prose_names and name not in prose_tokens:
            missing.add(name)
    return considered, missing


def lock_violations(bible: dict[str, Any], prose: str) -> tuple[str, ...]:
    """Asset-locked fields whose distinctive words are missing from the prose.

    Deliberately token-based rather than semantic: it must be cheap enough to run
    after every generation, and it only needs to catch *drift*, which shows up as
    vocabulary simply going missing. A field passes when the prose carries most
    of its distinctive words -- exact phrasing is expected to vary, since the
    expander rewrites freely around the facts.
    """
    prose_tokens = _tokens(prose)
    # Names are matched exactly and the description by stem: a name is the handle
    # the user steers with, and stemming one could let a near-miss pass for it.
    prose_stems = {stem_token(token) for token in prose_tokens}
    missing = []
    for key in locked_fields(bible):
        wanted = {stem_token(token) for token in _tokens(bible["fields"][key])}
        if not wanted:
            continue
        names, dropped_names = name_check(bible["fields"][key], prose)
        if dropped_names:
            missing.append(key)
            continue
        # A surviving name is strong evidence the subject was not replaced, so
        # the descriptive bar drops once every name is accounted for. Without
        # this, a legitimate rewording trips the audit: "Bob, a man in his late
        # 30s wearing a faded denim jacket" rendered as "Bob, his faded denim
        # jacket damp, climbs the stairs" keeps 4 of 7 tokens (0.57) purely
        # because the prose drops filler like "wearing" and "late". A false
        # positive costs a repair turn that corrects nothing.
        threshold = 0.35 if names else 0.6
        hits = len(wanted & prose_stems)
        if hits / len(wanted) < threshold:
            missing.append(key)
    return tuple(missing)
