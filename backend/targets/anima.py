"""Anima -- booru-tag anime stills, with a variant trap worth encoding.

Written from the official CircleStone Labs / Comfy Org model card, vendored and
hash-pinned. Two things make Anima unlike every other target here:

1. Its native interface is a tag list in a fixed section order PLUS a separate
   negative prompt, so the writer returns two fields, not one.
2. The rules move by variant, and they fail silently. `score_*` tags are correct
   on Anima-Base, actively harmful on Anima-Aesthetic (the card: they "push it
   too hard into slop territory"), and inert on Anima-Turbo, which runs at CFG 1
   where the negative prompt does nothing at all. Nothing in the output looks
   wrong when you get this wrong -- which is exactly why the audit checks it.
"""
from __future__ import annotations

import re
from typing import Any

from .base import Mode, Target, shared_checks, strip_fence

VARIANTS = ("base", "aesthetic", "turbo")
VARIANT_LABELS = {
    "base": "Anima-Base",
    "aesthetic": "Anima-Aesthetic",
    "turbo": "Anima-Turbo",
}

BASE_CONTRACT = (
    "Turn the user's brief into an Anima prompt pair and return only the pair, in exactly this shape and "
    "nothing else:\n"
    "Positive: <prompt>\n"
    "Negative: <prompt>\n"
    "Follow the supplied official Anima model-card prompting rules. Write Danbooru-style tags in the "
    "official section order: quality, meta, year and safety tags first, then the subject count tag such as "
    "1girl or 1boy, then character, then series, then artist, then general tags. Use lowercase with spaces "
    "and never underscores, except score tags, which are the only tags that use them. Prefix every artist "
    "tag with @, which is required for the artist to have any effect. Prefer the Gelbooru form when a tag "
    "differs between boorus. Tag weighting needs a high weight, for example (chibi:2). Keep each of the two "
    "lines on a single line, comma-separated, with no headings, bullets, JSON, Markdown or commentary."
)
VARIANT_CLAUSES = {
    "base": (
        "Anima-Base is selected: it is a true base model with no aesthetic tuning, so quality and artist tags "
        "do most of the work of giving it a look. Include a human-score quality tag and a score_* tag in the "
        "positive prompt, and their low-quality counterparts in the negative prompt."
    ),
    "aesthetic": (
        "Anima-Aesthetic is selected: it was fine-tuned with quality tags stripped from its captions. Never "
        "use a score_* tag in the positive OR the negative prompt -- on this variant they push the model "
        "toward slop. \"masterpiece, best quality\" may stay in the positive prompt."
    ),
    "turbo": (
        "Anima-Turbo is selected and runs at CFG 1, so the negative prompt has no effect on the image: still "
        "return a conventional negative line, but put everything that matters in the positive prompt rather "
        "than relying on the negative to remove it."
    ),
}
FAITHFUL_CLAUSE = (
    "Tag only what the brief supports. Do not invent characters, series, artists, props or settings it does "
    "not mention, and do not pad the tag list."
)
STORY_CLAUSE = (
    "Where the brief leaves something unspecified, fill it in: pose, expression, hair and clothing detail, "
    "background, framing and lighting tags are all fair game, and the model was trained with random tag "
    "dropout so a fuller list is safe. After the tag list you may add one or two natural-language sentences "
    "describing the scene, composition and lighting; when you name a character, describe their appearance too. "
    "Never contradict a fact the user gave or a fact established from their reference media."
)
SAFE_CLAUSE = (
    "Include the safe tag in the positive prompt, and include nsfw, explicit in the negative prompt."
)
NSFW_CLAUSE = (
    "Choose the safety tag that matches the brief -- safe, sensitive, nsfw or explicit -- and do not add safe "
    "to a brief that asks for adult content. Do not place nsfw or explicit in the negative prompt unless the "
    "user asks for that. Adult content is permitted when the brief calls for it: tag it plainly, without "
    "euphemism or refusal, and never add sexual content the brief does not ask for."
)

LABEL = re.compile(r"(?im)^\s*\**\s*(positive|negative)(?:\s+prompt)?\s*\**\s*:\s*\**\s*")
SCORE_TAG = re.compile(r"(?i)\bscore_(?:\d|up|down)\b")
UNDERSCORE_TOKEN = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b", re.IGNORECASE)
EXPLICIT_INTENT = re.compile(
    r"(?i)\b(?:nsfw|explicit|nude|nudity|naked|topless|sex|sexual|erotic|lewd|hentai|porn)\w*\b"
)
MIN_TAGS = 6
RECOMMENDED_NEGATIVES = ("worst quality", "low quality")


def system_prompt(mode: str, *, nsfw: bool = True, story: bool = True, variant: str | None = None) -> str:
    resolved = variant if variant in VARIANTS else "turbo"
    parts = [
        BASE_CONTRACT,
        VARIANT_CLAUSES[resolved],
        STORY_CLAUSE if story else FAITHFUL_CLAUSE,
        NSFW_CLAUSE if nsfw else SAFE_CLAUSE,
    ]
    return " ".join(parts)


def parse_output(text: str) -> dict[str, Any]:
    """Split the two blocks. An unlabelled answer yields an empty negative.

    Deliberately not lenient about a missing Negative label: the audit treats it
    as a repairable contract violation rather than guessing where the split was.
    """
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
        "Return only the two labelled lines, Positive: then Negative:, with tags in the official section "
        "order and nothing else. "
        + (
            "Tag the scene fully; the model was trained with tag dropout, so a rich list is safe."
            if story else
            "Tag only what the brief supports."
        )
    )


def _items(block: str) -> list[str]:
    return [item.strip() for item in (block or "").split(",") if item.strip()]


def _tag_items(block: str) -> list[str]:
    """Items up to the first sentence-like one.

    Story builder is allowed to append natural-language sentences, and those are
    legitimately capitalised -- so the lowercase and underscore rules apply only
    to the tag region, not to prose the card explicitly permits.
    """
    tags: list[str] = []
    for item in _items(block):
        if "." in item or len(item.split()) > 6:
            break
        tags.append(item)
    return tags


def _intent_text(assembled: dict[str, Any]) -> str:
    source = assembled.get("input", {})
    parts = [str(source.get("creative_brief") or ""), str(source.get("instruction") or "")]
    doc = source.get("generic")
    if isinstance(doc, dict):
        from .. import generic

        parts.append(generic.render(doc))
    return "\n".join(part for part in parts if part)


def audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
    source = assembled["input"]
    variant = source.get("variant") if source.get("variant") in VARIANTS else "turbo"
    nsfw = bool(source.get("nsfw", True))
    parsed = parse_output(prompt)
    positive, negative = parsed["prompt"], parsed["negative_prompt"]
    positive_tags = _tag_items(positive)
    positive_items = _items(positive)
    lowered_positive = positive.lower()
    lowered_negative = negative.lower()

    failures: list[str] = []
    warnings: list[str] = []

    if not positive:
        failures.append("the Positive: line is missing or empty")
    if not negative:
        failures.append("the Negative: line is missing or empty")
    if positive and len(positive_items) < MIN_TAGS:
        failures.append(f"the positive prompt has only {len(positive_items)} tags; Anima wants a fuller tag list")

    bad_underscores = sorted({
        token for token in UNDERSCORE_TOKEN.findall(", ".join(positive_tags) + ", " + ", ".join(_tag_items(negative)))
        if not SCORE_TAG.fullmatch(token)
    })
    if bad_underscores:
        failures.append(
            "tags must use spaces, not underscores (score tags excepted): " + ", ".join(bad_underscores)
        )
    uppercase_tags = [item for item in positive_tags if re.search(r"[A-Z]", item) and not item.startswith("@")]
    if uppercase_tags:
        failures.append("tags must be lowercase: " + ", ".join(sorted(uppercase_tags)))
    misplaced_artist = [item for item in positive_items if "@" in item and not item.startswith("@")]
    if misplaced_artist:
        failures.append("an artist tag must start with @: " + ", ".join(sorted(misplaced_artist)))

    score_tags = sorted(set(SCORE_TAG.findall(positive) + SCORE_TAG.findall(negative)))
    if variant == "aesthetic" and score_tags:
        failures.append(
            f"{VARIANT_LABELS['aesthetic']} must not use score tags in either prompt: " + ", ".join(score_tags)
        )
    if variant == "base" and not score_tags:
        warnings.append("no score tag; Anima-Base leans on quality and score tags for its look")

    explicit_requested = bool(EXPLICIT_INTENT.search(_intent_text(assembled)))
    safety_in_positive = [tag for tag in ("safe", "sensitive", "nsfw", "explicit") if tag in _items(lowered_positive)]
    if not nsfw:
        if "safe" not in safety_in_positive:
            failures.append("with Naughty off the positive prompt must include the safe tag")
        missing_negative_safety = [tag for tag in ("nsfw", "explicit") if tag not in _items(lowered_negative)]
        if missing_negative_safety and variant != "turbo":
            failures.append(
                "with Naughty off the negative prompt must include " + ", ".join(missing_negative_safety)
            )
        elif missing_negative_safety:
            warnings.append(
                "negative prompt is missing " + ", ".join(missing_negative_safety)
                + f" ({VARIANT_LABELS['turbo']} ignores the negative prompt, so tag it in the positive instead)"
            )
    elif explicit_requested and "safe" in safety_in_positive:
        failures.append("the brief asks for adult content but the positive prompt is tagged safe")
    elif not safety_in_positive:
        warnings.append("no safety tag; the card recommends tagging safety explicitly")

    if variant == "turbo" and negative:
        warnings.append(f"{VARIANT_LABELS['turbo']} runs at CFG 1, so the negative prompt has no effect")
    elif negative and not any(item in lowered_negative for item in RECOMMENDED_NEGATIVES):
        warnings.append("negative prompt omits the card's recommended quality negatives")

    result: dict[str, Any] = {
        "mode": source["mode"],
        "output_shape": "tags",
        "variant": variant,
        "variant_label": VARIANT_LABELS[variant],
        "positive_tag_count": len(positive_items),
        "negative_present": bool(negative),
        "score_tags": score_tags,
        "safety_tags": safety_in_positive,
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
    violations = audit_result.get("shared_failures") or ["Anima format audit"]
    variant = audit_result.get("variant", "turbo")
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "This is a narrow correction pass, not a new prompt. Correct only the listed violations and "
                    "keep every other tag, order, weight and creative choice exactly as it is. Return the "
                    "complete corrected pair in the required shape:\nPositive: <prompt>\nNegative: <prompt>\n"
                    f"The selected variant is {VARIANT_LABELS.get(variant, variant)}. "
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


ANIMA = Target(
    id="anima",
    label="Anima",
    vendor="CircleStone Labs",
    workspace="image",
    media_kind="image",
    modes=(
        Mode(
            "Anima",
            "Anima tag prompt",
            "as an Anima tag pair",
            ("anima",),
            {"image": 4},
            "anima",
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
    final_contract=final_contract,
    parse_output=parse_output,
    variants=VARIANTS,
    default_variant="turbo",
)
