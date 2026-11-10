"""Natural-language image targets whose vendors publish the expansion prompt itself.

Qwen-Image, FLUX 3 Image Klein and MOSS-Image all take one descriptive caption,
and each vendor ships the system prompt its own prompt-rewriter runs -- the text
the image model was trained to receive. So the writer's job is the vendor's job:
the vendored guide carries the register, and this module only adds the shared
contract (one caption, no commentary), the Naughty and Story dials, and an audit
for the failures that look fine and render wrong.

One factory rather than three copies of `krea2.py`: these targets differ in their
guide, their length band and whether a negative prompt exists -- not in shape.
"""
from __future__ import annotations

import re
from typing import Any

from .. import heat as heat_levels
from .base import Mode, Target, append_facts, shared_checks, strip_fence

META_PREAMBLE = re.compile(
    r"(?i)^\s*(?:sure[,!.]|certainly[,!.]|here(?:'s| is)\b|rewritten prompt\s*:|caption\s*:|prompt\s*:)"
)
STRUCTURE_LINE = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+•]\s|\d+[.)]\s|\||>\s)")
JSONISH = re.compile(r'^\s*[{\[]|"(?:rewritten_prompt|caption|wh_ratio)"\s*:')
WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")

FAITHFUL_CLAUSE = (
    "Do not add people, objects, text or scene details the brief does not support; describe only what it "
    "establishes, in full."
)
STORY_CLAUSE = (
    "Where the brief leaves something open, decide it and commit to one concrete choice -- appearance, "
    "setting, light, palette and composition -- without hedging between alternatives. Never contradict a "
    "fact the user gave or one established from their reference media."
)
DIGNITY_CLAUSE = "Keep the image safe for work."


def _contract(label: str, min_words: int, max_words: int) -> str:
    return (
        f"Turn the user's brief into one {label} caption and return only that caption. Follow the supplied "
        f"official {label} prompting instructions for register, ordering and detail, but ignore any output "
        "format they specify (JSON, an aspect-ratio field, a file): the answer here is the caption text alone, "
        "with no label, quotes, Markdown, headings, lists or commentary. Write in English, keeping any text "
        "that must appear in the image in its own language and inside double quotes, exactly as given. Keep "
        "LoRA tags such as <lora:name:0.8>, embedding names and trigger words exactly as the user wrote them. "
        f"Aim for {min_words} to {max_words} words."
    )


def make_prose_target(
    *,
    target_id: str,
    label: str,
    vendor: str,
    mode_id: str,
    guide_ids: tuple[str, ...],
    min_words: int,
    max_words: int,
    negative: str = "",
    note: str = "",
) -> Target:
    """`negative` is the vendor's recommended negative prompt, returned verbatim;
    empty means the model takes none (distilled / guidance-free pipelines)."""
    base_contract = _contract(label, min_words, max_words) + (f" {note}" if note else "")

    def system_prompt(
        mode: str, *, nsfw: bool = True, story: bool = True, variant: str | None = None, heat: int | None = None,
    ) -> str:
        level = heat_levels.resolve(heat=heat, nsfw=nsfw)
        return " ".join([
            base_contract,
            STORY_CLAUSE if story else FAITHFUL_CLAUSE,
            DIGNITY_CLAUSE if level == heat_levels.CLEAN else heat_levels.clause(level),
        ])

    def final_contract(mode: str, task_text: str = "", *, story: bool = True) -> str:
        return (
            f"Return only the finished {label} caption: prose, no labels, quotes around it, JSON or commentary. "
            + ("Commit to every open choice." if story else "Add nothing the brief does not support.")
        )

    def parse_output(text: str) -> dict[str, Any]:
        return {"prompt": strip_fence(text), "negative_prompt": negative}

    def audit(prompt: str, assembled: dict[str, Any]) -> dict[str, Any]:
        text = strip_fence(prompt)
        words = len(WORD.findall(text))
        failures: list[str] = []
        warnings: list[str] = []
        if JSONISH.search(text):
            failures.append("return the caption text itself, not JSON (the vendor guide's output format does not apply)")
        if STRUCTURE_LINE.search(text) or "```" in text:
            failures.append("the caption must be prose, without headings, lists or code fences")
        if META_PREAMBLE.match(text):
            failures.append("the caption must start with the image itself, not a preamble or label")
        if words < min_words // 2:
            failures.append(f"the caption is too short for {label} at {words} words")
        elif words < min_words:
            warnings.append(f"short caption ({words} words; {label} expects {min_words}-{max_words})")
        if words > max_words * 1.5:
            warnings.append(f"very long caption ({words} words)")
        result: dict[str, Any] = {
            "mode": assembled["input"]["mode"],
            "output_shape": "prose",
            "word_count": words,
            "format_failures": failures,
            "quality_warnings": warnings,
            "quality_target_pass": not warnings,
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
        assembled: dict[str, Any], messages: list[dict[str, Any]], prompt: str, audit_result: dict[str, Any],
    ) -> dict[str, Any]:
        violations = audit_result.get("shared_failures") or [f"{label} format audit"]
        return {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "This is a narrow correction pass, not a new caption. Correct only the listed violations "
                        "and keep every other fact, subject, colour, quoted text and creative choice exactly as it "
                        f"is. Return the complete corrected {label} caption as prose with no commentary. "
                        "Violations: " + "; ".join(violations)
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
        assembled: dict[str, Any], prompt: str, repaired: str, plan: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str | None]:
        repaired_audit = audit(repaired, assembled)
        if repaired_audit.get("repair_required") is True:
            return False, repaired_audit, "repaired draft still failed: " + ", ".join(repaired_audit.get("shared_failures") or [])
        return True, repaired_audit, None

    return Target(
        id=target_id,
        label=label,
        vendor=vendor,
        workspace="image",
        media_kind="image",
        modes=(Mode(mode_id, f"{label} image prompt", f"as one {label} caption", guide_ids, {"image": 4}, target_id),),
        fields=frozenset({"aspect_ratio", "nsfw", "story"} | ({"negative_prompt"} if negative else set())),
        output_shape="prose",
        brief_limit=8000,
        output_tokens="image",
        system_prompt=system_prompt,
        audit=audit,
        repair_plan=repair_plan,
        accept_repair=accept_repair,
        integrate_facts=append_facts,
        final_contract=final_contract,
        parse_output=parse_output,
    )


# Qwen's own rewriter targets ~200 words for a portrait and more for dense text
# layouts; Qwen-Image 2512 and 2.1 read the same register.
QWEN_IMAGE = make_prose_target(
    target_id="qwenimage",
    label="Qwen-Image",
    vendor="Qwen",
    mode_id="QwenImage",
    guide_ids=("qwen_image",),
    min_words=120,
    max_words=350,
    note=(
        "When the image contains no text, the guide asks for the sentence \"The image contains no recognizable "
        "text.\"; include it."
    ),
)
# BFL: the model is trained on its upsampler's captions, which run long and dense.
FLUX3 = make_prose_target(
    target_id="flux3",
    label="FLUX 3",
    vendor="Black Forest Labs",
    mode_id="Flux3",
    guide_ids=("flux3",),
    min_words=150,
    max_words=450,
)
# MOSS-Image's guide recommends "nsfw, worst quality, bad anatomy, watermark,
# jpeg artifact". `nsfw` is left out: the Naughty dial decides that, and a fixed
# negative would silently fight an adult brief.
MOSS_NEGATIVE = "worst quality, bad anatomy, watermark, jpeg artifact"
MOSS_IMAGE = make_prose_target(
    target_id="mossimage",
    label="MOSS-Image",
    vendor="OpenMOSS",
    mode_id="MossImage",
    guide_ids=("moss_image", "moss_image_enhancer"),
    min_words=60,
    max_words=250,
    negative=MOSS_NEGATIVE,
)
