"""SD tags -- comma-tag prompts for the Stable Diffusion family (SDXL, Illustrious, Pony, SD 1.5).

These four share one interface -- a comma-separated tag list with `(word:1.2)`
attention weights plus a negative prompt -- and differ only in their quality
vocabulary, which is why they are one target with four variants rather than
four targets. The variant is where the silent failures live:

- Pony Diffusion V6 is trained on the full `score_9, score_8_up, ...` string
  and its card says plain "masterpiece"-style quality tags do nothing for it.
- Illustrious is the reverse: Danbooru tags with "masterpiece, best quality",
  and no score tags at all.
- SD 1.5 reads 75 tokens per CLIP chunk; anything after the first chunk is
  weaker, so the important tags go first and the list stays short.

Written from the vendored Illustrious XL model card and the Pony Diffusion V6
model description. Plain SDXL and SD 1.5 publish no prompting guide; their
clauses below are this writer's own contract, not passed off as official.

LoRA trigger words, `<lora:...>` and embedding tokens in the brief are to be
copied verbatim; the contract says so, because one changed character and the
LoRA silently stops firing.
"""
from __future__ import annotations

import re
from typing import Any

from .. import heat as heat_levels
from .base import Mode, Target, shared_checks, strip_fence

VARIANTS = ("sdxl", "illustrious", "pony", "sd1")
VARIANT_LABELS = {
    "sdxl": "SDXL",
    "illustrious": "Illustrious XL",
    "pony": "Pony Diffusion V6 XL",
    "sd1": "Stable Diffusion 1.5",
}
PONY_SCORE_PREFIX = "score_9, score_8_up, score_7_up, score_6_up, score_5_up, score_4_up"

BASE_CONTRACT = (
    "Turn the user's brief into a Stable Diffusion prompt pair and return only the pair, in exactly this "
    "shape and nothing else:\n"
    "Positive: <prompt>\n"
    "Negative: <prompt>\n"
    "Write comma-separated tags and short phrases, most important first. Use (tag:1.2) to strengthen a tag "
    "and keep weights between 0.5 and 1.5; never stack nested parentheses. Copy LoRA tags such as "
    "<lora:name:0.8>, embedding names and trigger words exactly as the user wrote them, character for "
    "character. Keep each of the two lines on a single line, with no headings, bullets, JSON, Markdown or "
    "commentary."
)
VARIANT_CLAUSES = {
    "sdxl": (
        "SDXL is selected: it understands short natural phrases as well as tags. Lead with the subject and "
        "the medium (photo, illustration, oil painting), then appearance, clothing, pose, setting, lighting "
        "and camera. Do not use score_ tags."
    ),
    "illustrious": (
        "Illustrious XL is selected: follow the supplied official Illustrious model card. Write Danbooru tags "
        "with spaces instead of underscores, starting with the subject count tag such as 1girl or 1boy, and "
        "put quality tags like masterpiece, best quality at the front of the positive prompt. Use one "
        "composition tag such as upper body, cowboy shot, portrait or full body, not several. Never use "
        "score_ tags. The negative prompt should follow the card's examples: worst quality, bad quality, low "
        "quality, lowres and the anatomy and artifact tags that fit."
    ),
    "pony": (
        "Pony Diffusion V6 XL is selected: follow the supplied official Pony model description. The positive "
        f"prompt must begin with exactly this string: {PONY_SCORE_PREFIX}. After it, add a source_ tag "
        "(source_anime, source_cartoon, source_furry or source_pony) when the style is clear, then a "
        "rating_ tag, then describe the image in tags and short phrases. Do not add masterpiece, best "
        "quality, hd or similar quality tags; the score string replaces them. Keep the negative prompt short."
    ),
    "sd1": (
        "Stable Diffusion 1.5 is selected: it reads only about 75 tokens at full strength, so keep the "
        "positive prompt under roughly 60 words and put the subject and the most important details in the "
        "first dozen tags. Quality tags such as masterpiece, best quality and highly detailed help. Do not use "
        "score_ tags."
    ),
}
FAITHFUL_CLAUSE = (
    "Tag only what the brief supports. Do not invent characters, props or settings it does not mention."
)
STORY_CLAUSE = (
    "Where the brief leaves something unspecified, fill it in with tags: pose, expression, hair and clothing "
    "detail, background, framing and lighting. Never contradict a fact the user gave or a fact established "
    "from their reference media."
)
SAFE_CLAUSE = "Keep the image safe for work and put nsfw, nude in the negative prompt."
NSFW_CLAUSE = (
    "Adult content is permitted when the brief calls for it: tag it plainly, without euphemism or refusal, "
    "and never add sexual content the brief does not ask for. Do not put nsfw in the negative prompt unless "
    "the user asks for that."
)

LABEL = re.compile(r"(?im)^\s*\**\s*(positive|negative)(?:\s+prompt)?\s*\**\s*:\s*\**\s*")
SCORE_TAG = re.compile(r"(?i)\bscore_\d(?:_up)?\b")
NESTED_WEIGHT = re.compile(r"\(\s*\(")
MIN_TAGS = 5
SD1_WORD_LIMIT = 75


def _variant(source: dict[str, Any]) -> str:
    value = source.get("variant")
    return value if value in VARIANTS else "illustrious"


def system_prompt(
    mode: str, *, nsfw: bool = True, story: bool = True, variant: str | None = None, heat: int | None = None,
) -> str:
    resolved = variant if variant in VARIANTS else "illustrious"
    level = heat_levels.resolve(heat=heat, nsfw=nsfw)
    parts = [
        BASE_CONTRACT,
        VARIANT_CLAUSES[resolved],
        STORY_CLAUSE if story else FAITHFUL_CLAUSE,
        *((SAFE_CLAUSE,) if level == heat_levels.CLEAN else (NSFW_CLAUSE, heat_levels.clause(level))),
    ]
    return " ".join(parts)


def parse_output(text: str) -> dict[str, Any]:
    value = strip_fence(text)
    matches = list(LABEL.finditer(value))
    if not matches:
        return {"prompt": value.strip(), "negative_prompt": ""}
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        blocks[match.group(1).lower()] = value[match.end():end].strip()
    return {
        "prompt": blocks.get("positive", "").strip(),
        "negative_prompt": blocks.get("negative", "").strip(),
    }


def final_contract(mode: str, task_text: str = "", *, story: bool = True) -> str:
    return (
        "Return only the two labelled lines, Positive: then Negative:, and nothing else. "
        + ("Tag the scene fully." if story else "Tag only what the brief supports.")
    )


def _items(block: str) -> list[str]:
    return [item.strip() for item in (block or "").split(",") if item.strip()]


def audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
    source = assembled["input"]
    variant = _variant(source)
    parsed = parse_output(prompt)
    positive, negative = parsed["prompt"], parsed["negative_prompt"]
    items = _items(positive)
    failures: list[str] = []
    warnings: list[str] = []

    if not positive:
        failures.append("the Positive: line is missing or empty")
    if not negative and variant != "pony":
        failures.append("the Negative: line is missing or empty")
    if positive and len(items) < MIN_TAGS:
        failures.append(f"the positive prompt has only {len(items)} tags; write a fuller tag list")
    if NESTED_WEIGHT.search(positive):
        failures.append("use (tag:1.2) weights, not nested parentheses")

    score_tags = sorted(set(SCORE_TAG.findall(positive) + SCORE_TAG.findall(negative)))
    if variant == "pony":
        if not positive.lower().startswith(PONY_SCORE_PREFIX):
            failures.append(f"Pony prompts must begin with exactly: {PONY_SCORE_PREFIX}")
        if re.search(r"(?i)\b(masterpiece|best quality)\b", positive):
            warnings.append("Pony ignores masterpiece / best quality; the score string replaces them")
    elif score_tags:
        failures.append(f"{VARIANT_LABELS[variant]} must not use score tags: " + ", ".join(score_tags))
    if variant == "illustrious" and "_" in re.sub(r"<[^>]*>", "", positive) and not score_tags:
        warnings.append("Illustrious tags use spaces, not underscores")
    if variant == "sd1" and len(positive.split()) > SD1_WORD_LIMIT:
        warnings.append(f"the positive prompt is {len(positive.split())} words; SD 1.5 weakens past ~75 tokens")

    result: dict[str, Any] = {
        "mode": source["mode"],
        "output_shape": "tags",
        "variant": variant,
        "variant_label": VARIANT_LABELS[variant],
        "positive_tag_count": len(items),
        "negative_present": bool(negative),
        "score_tags": score_tags,
        "format_failures": failures,
        "quality_warnings": warnings,
        "quality_target_pass": not warnings,
        "reference_understanding": "not_applicable",
    }
    shared = shared_checks(assembled, positive + "\n" + negative)
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
    violations = audit_result.get("shared_failures") or ["SD tag format audit"]
    variant = audit_result.get("variant", "illustrious")
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "This is a narrow correction pass, not a new prompt. Correct only the listed violations and "
                    "keep every other tag, order, weight, LoRA tag and trigger word exactly as it is. Return the "
                    "complete corrected pair in the required shape:\nPositive: <prompt>\nNegative: <prompt>\n"
                    f"{VARIANT_CLAUSES.get(variant, '')} Violations: " + "; ".join(violations)
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


def integrate_facts(assembled: dict[str, Any], prompt: str, additions: list[tuple[str, str]]) -> str:
    """Restore dropped facts on the end of the POSITIVE line, never under Negative:."""
    from .. import fact_insertion

    negative = next((m for m in LABEL.finditer(prompt) if m.group(1).lower() == "negative"), None)
    head = prompt if negative is None else prompt[: negative.start()]
    for _key, sentence in additions:
        head = fact_insertion.append_sentence(head, sentence)
    return head if negative is None else f"{head.rstrip()}\n{prompt[negative.start():]}"


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


def guides_for_variant(variant: str | None) -> tuple[str, ...]:
    """Only the variant's own official guide; plain SDXL and SD 1.5 have none."""
    resolved = variant if variant in VARIANTS else "illustrious"
    return {"illustrious": ("illustrious",), "pony": ("pony",)}.get(resolved, ())


SDTAGS = Target(
    id="sdtags",
    label="SD tags",
    vendor="Stable Diffusion family",
    workspace="image",
    media_kind="image",
    modes=(
        Mode(
            "SDTags",
            "SD tag prompt",
            "as a Stable Diffusion tag pair (SDXL, Illustrious, Pony or SD 1.5)",
            ("illustrious", "pony"),
            {"image": 4},
            "sdtags",
        ),
    ),
    fields=frozenset({"aspect_ratio", "nsfw", "story", "variant", "negative_prompt"}),
    output_shape="tags",
    brief_limit=8000,
    output_tokens="image",
    system_prompt=system_prompt,
    audit=audit,
    repair_plan=repair_plan,
    accept_repair=accept_repair,
    integrate_facts=integrate_facts,
    final_contract=final_contract,
    parse_output=parse_output,
    variants=VARIANTS,
    default_variant="illustrious",
    guides_for_variant=guides_for_variant,
)
