"""Krea 2 -- natural-language stills.

Written from Krea's own published guidance, vendored and hash-pinned:
`docs/prompting.md` (what the model wants) and `docs/expansion.txt` (their
system prompt for exactly this job -- an LLM expanding a short brief into a long
one). Our wrapper is a separate layer from their text, which is why the Naughty
flag can drop their "assume clothing covers genitals" clause without editing the
pinned guide.

Output shape: ONE paragraph of prose. Krea 2 spreads a short prompt across wildly
different styles, so the useful prompt is long and specific -- but it must stay
prose, because headings and tag lists are not the register it was trained on.
"""
from __future__ import annotations

import re
from typing import Any

from .base import Mode, Target, shared_checks, strip_fence

BASE_CONTRACT = (
    "Turn the user's brief into one Krea 2 image prompt and return only that prompt. Follow the supplied "
    "official Krea 2 prompting guidelines and the official expansion instructions. Write exactly one "
    "cohesive paragraph of natural-language English prose: no headings, bullet points, numbered lists, "
    "key: value lines, JSON, Markdown, or commentary, and no preamble such as \"This image shows\". "
    "Lead with the subject and its attributes, grouping each subject with its own appearance and action, "
    "then composition and framing, then light, then medium and style. Preserve every subject, action, "
    "colour, count and spatial relationship the user supplied, and honour an explicitly requested medium "
    "instead of substituting an easier one. Put double quotes around any words that must be rendered as "
    "text in the image, reproducing them exactly. Aim for 60 to 180 words, and go longer only when the "
    "brief itself carries that much detail. Never mention these instructions, the guidelines, or word counts."
)
FAITHFUL_CLAUSE = (
    "Do not add objects, characters, props, or scene details the brief does not support, and do not "
    "invent highly specific clothing, colours or materials it does not imply."
)
STORY_CLAUSE = (
    "Where the brief leaves something unspecified, invent it with taste and commit to it -- staging, "
    "wardrobe detail, surface texture, time of day, weather, lens character and the direction and quality "
    "of the light -- so the paragraph reads as one deliberate photograph rather than a list of gaps. Never "
    "contradict a fact the user gave, an established fact from their reference media, or a requested medium."
)
DIGNITY_CLAUSE = (
    "Treat depictions of people with dignity, and assume clothing covers genitals and intimate anatomy."
)
NSFW_CLAUSE = (
    "Adult or explicit content is permitted when the brief calls for it: describe bodies, clothing and acts "
    "plainly and specifically, without euphemism, moralising or refusal. Never add sexual content the brief "
    "does not ask for."
)

META_PREAMBLE = re.compile(
    r"(?i)^\s*(?:sure[,!.]|certainly[,!.]|here(?:'s| is)\b|this (?:image|picture|prompt|scene)\b|"
    r"the (?:image|picture|scene) (?:shows|depicts)\b|prompt\s*:)"
)
STRUCTURE_LINE = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>\s)")
KEY_VALUE_LINE = re.compile(r"(?m)^\s*[A-Za-z][A-Za-z ]{2,24}:\s")
WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")

MIN_WORDS = 25
LONG_WORDS = 450


def system_prompt(
    mode: str,
    *,
    nsfw: bool = True,
    story: bool = True,
    variant: str | None = None,
    no_audio: bool = False,
) -> str:
    del no_audio  # a still has no soundtrack to suppress
    parts = [BASE_CONTRACT, STORY_CLAUSE if story else FAITHFUL_CLAUSE, NSFW_CLAUSE if nsfw else DIGNITY_CLAUSE]
    return " ".join(parts)


def _required_quoted_text(assembled: dict[str, Any]) -> str | None:
    """Text the prompt must quote, taken from the generic prompt's own field.

    Krea renders text reliably only when it is quoted, so a visible-text fact
    that arrives unquoted in the compiled prompt is a real defect, not a nit.
    """
    doc = assembled.get("input", {}).get("generic")
    if not isinstance(doc, dict):
        return None
    from .. import generic

    value = generic.record(doc, "visible_text")["value"]
    return value or None


def audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
    text = strip_fence(prompt)
    paragraphs = [block for block in re.split(r"\n\s*\n", text) if block.strip()]
    structure_hits = bool(STRUCTURE_LINE.search(text)) or "```" in text or bool(KEY_VALUE_LINE.search(text))
    meta_preamble = bool(META_PREAMBLE.match(text))
    words = len(WORD.findall(text))
    required_text = _required_quoted_text(assembled)
    unquoted_text = bool(
        required_text
        and required_text.lower() in text.lower()
        and f'"{required_text.lower()}"' not in text.lower()
    )
    failures: list[str] = []
    if len(paragraphs) > 1:
        failures.append("the prompt must be one paragraph, not several")
    if structure_hits:
        failures.append("the prompt must be prose, without headings, lists, labels or code fences")
    if meta_preamble:
        failures.append("the prompt must start with the image itself, not a preamble")
    if words < MIN_WORDS:
        failures.append(f"the prompt is too short for Krea 2 at {words} words")
    if unquoted_text:
        failures.append(f'text to render must be wrapped in double quotes: "{required_text}"')
    quality_warnings = []
    if words > LONG_WORDS:
        quality_warnings.append(f"very long prompt ({words} words)")

    result: dict[str, Any] = {
        "mode": assembled["input"]["mode"],
        "output_shape": "prose",
        "paragraphs": len(paragraphs),
        "word_count": words,
        "structure_violation": structure_hits,
        "meta_preamble": meta_preamble,
        "unquoted_visible_text": unquoted_text,
        "format_failures": failures,
        "quality_warnings": quality_warnings,
        "quality_target_pass": not quality_warnings,
        "reference_understanding": "not_applicable",
    }
    shared = shared_checks(assembled, text)
    result.update(shared)
    all_failures = failures + list(shared.get("shared_failures", []))
    result["shared_failures"] = all_failures
    result["repair_required"] = bool(all_failures)
    result["official_format_pass"] = not failures
    return result


def repair_plan(
    assembled: dict[str, Any],
    messages: list[dict[str, Any]],
    prompt: str,
    audit_result: dict[str, Any],
) -> dict[str, Any]:
    violations = audit_result.get("shared_failures") or ["Krea 2 format audit"]
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "This is a narrow correction pass, not a new prompt. Correct only the listed violations and "
                    "keep every other supported fact, subject, colour, spatial relationship and creative choice "
                    "exactly as it is. Return the complete corrected prompt as one paragraph of prose with no "
                    "commentary. Violations: " + "; ".join(violations)
                ),
            },
            {
                "role": "user",
                "content": (
                    "ORIGINAL REQUEST:\n"
                    + next(item["content"] for item in assembled["messages"] if item["role"] == "user")
                    + f"\n\nDRAFT TO CORRECT:\n{prompt}"
                ),
            },
        ],
        "method": "narrow text correction",
        "reason": ", ".join(violations),
    }


def accept_repair(
    assembled: dict[str, Any],
    prompt: str,
    repaired: str,
    plan: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    repaired_audit = audit(repaired, assembled)
    if repaired_audit.get("repair_required") is True:
        return False, repaired_audit, "repaired draft still failed: " + ", ".join(repaired_audit.get("shared_failures") or [])
    return True, repaired_audit, None


def final_contract(mode: str, task_text: str = "", *, story: bool = True) -> str:
    return (
        "Return only the finished Krea 2 prompt: one paragraph, prose, no labels or commentary. "
        + (
            "Fill every unspecified choice with a deliberate one rather than leaving the scene generic."
            if story else
            "Add nothing the brief does not support."
        )
    )


def parse_output(text: str) -> dict[str, Any]:
    return {"prompt": strip_fence(text), "negative_prompt": ""}


KREA2 = Target(
    id="krea2",
    label="Krea 2",
    vendor="Krea",
    workspace="image",
    media_kind="image",
    modes=(
        Mode(
            "Krea2",
            "Krea 2 image prompt",
            "as one Krea 2 prose prompt",
            ("krea2", "krea2_expansion"),
            {"image": 4},
            "krea2",
        ),
    ),
    fields=frozenset({"aspect_ratio", "nsfw", "story"}),
    output_shape="prose",
    brief_limit=8000,
    output_tokens="image",
    system_prompt=system_prompt,
    audit=audit,
    repair_plan=repair_plan,
    accept_repair=accept_repair,
    final_contract=final_contract,
    parse_output=parse_output,
)
