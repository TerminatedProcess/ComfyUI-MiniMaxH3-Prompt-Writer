"""Target descriptions: one spec per generation model the writer can write for.

A target owns everything that differs between models -- which guide it is
written from, what its system prompt says, what shape its output takes, how a
draft is audited, and how a failed draft is corrected. Everything a target does
NOT own (model backends, media handling, context planning, the repair loop
itself) stays generic, so adding a model is a spec plus a vendored guide rather
than another branch through the pipeline.

The hooks are plain callables rather than subclasses: they are pure functions of
the assembled request, which keeps them testable without a GPU or a server.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


# Request fields a target may declare. Anything not declared is neither required
# nor sent; this is what keeps Duration off an image target instead of an
# `if mode == ...` in the route handler.
FIELDS = frozenset({
    "duration_seconds",
    "aspect_ratio",
    "lyrics",
    "nsfw",
    "story",
    "no_audio",
    "variant",
    "negative_prompt",
})
OUTPUT_SHAPES = frozenset({"sections", "prose", "tags", "lyrics"})
WORKSPACES = frozenset({"video", "music", "image"})

FENCE = re.compile(r"^```(?:[a-zA-Z]*)\s*\n([\s\S]*?)\n```\s*$")


class TargetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Mode:
    """One way of asking a target for a prompt."""

    id: str
    label: str
    summary: str
    guide_ids: tuple[str, ...] = ()
    reference_limits: dict[str, int] = field(default_factory=dict)
    profile: str = "standard"
    # Music3Lyrics is a system-prompt profile, not something you can Generate.
    generation: bool = True


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    vendor: str
    workspace: str
    media_kind: str
    modes: tuple[Mode, ...]
    fields: frozenset[str]
    output_shape: str
    brief_limit: int
    output_tokens: str
    system_prompt: Callable[..., str]
    audit: Callable[[str, dict[str, Any]], dict[str, Any]]
    repair_plan: Callable[..., dict[str, Any]]
    accept_repair: Callable[..., tuple[bool, dict[str, Any], str | None]]
    variants: tuple[str, ...] = ()
    default_variant: str | None = None
    # The closing instruction appended to the user message. It restates the
    # contract where the model is most likely to still be paying attention.
    final_contract: Callable[..., str] | None = None
    parse_output: Callable[[str], dict[str, Any]] | None = None
    infer_mode: Callable[..., str] | None = None
    # Image targets take stills; the sequence workspace chunks a video timeline.
    supports_sequence: bool = False

    def __post_init__(self) -> None:
        if self.workspace not in WORKSPACES:
            raise TargetError("INVALID_TARGET", f"{self.id}: unknown workspace {self.workspace!r}")
        if self.output_shape not in OUTPUT_SHAPES:
            raise TargetError("INVALID_TARGET", f"{self.id}: unknown output shape {self.output_shape!r}")
        unknown = self.fields - FIELDS
        if unknown:
            raise TargetError("INVALID_TARGET", f"{self.id}: unknown fields {sorted(unknown)}")
        if self.variants and self.default_variant not in self.variants:
            raise TargetError("INVALID_TARGET", f"{self.id}: default variant is not one of its variants")

    @property
    def generation_modes(self) -> tuple[Mode, ...]:
        return tuple(mode for mode in self.modes if mode.generation)

    def mode(self, mode_id: str) -> Mode:
        for mode in self.modes:
            if mode.id == mode_id:
                return mode
        raise TargetError("INVALID_MODE", f"{self.id} has no mode {mode_id!r}")

    def declares(self, request_field: str) -> bool:
        return request_field in self.fields


def strip_fence(text: str) -> str:
    """Remove a Markdown code fence a model wrapped its answer in.

    Not a correctness check -- a fenced answer is otherwise valid, and failing it
    would spend a repair turn on packaging.
    """
    value = (text or "").strip()
    match = FENCE.fullmatch(value)
    return match.group(1).strip() if match else value


def compose(base: str, *, remove: tuple[str, ...] = (), add: tuple[str, ...] = ()) -> str:
    """Assemble a system prompt from a base contract plus flag-driven edits.

    Every removal must actually be present. A flag that silently fails to change
    the contract is worse than a crash: the UI would report Story builder on
    while the model still received the no-invention rule.
    """
    text = base
    for sentence in remove:
        if sentence not in text:
            raise TargetError(
                "SYSTEM_PROMPT_DRIFT",
                "A system-prompt clause this flag edits is no longer present in the base contract.",
            )
        text = text.replace(sentence, "")
    text = re.sub(r"  +", " ", text).strip()
    for sentence in add:
        text = f"{text} {sentence.strip()}"
    return text.strip()


def shared_checks(assembled: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Checks every target owes the user, whatever shape its output takes.

    Two of them: facts the generic prompt established must survive the compile,
    and standing goals must still hold. Both are re-run on every compile,
    because "it held for H3" says nothing about the Krea 2 compile that follows.
    """
    from .. import generic, goals as goal_ledger

    source = assembled.get("input", {})
    doc = source.get("generic")
    ledger = source.get("goals") or []
    result: dict[str, Any] = {}
    failures: list[str] = []

    if isinstance(doc, dict):
        missing = generic.lock_violations(doc, prompt)
        result["lock_violations"] = list(missing)
        result["lock_expected"] = generic.lock_expected(doc, missing)
        for key in missing:
            value = result["lock_expected"].get(key)
            label = generic.LABELS.get(key, key)
            failures.append(
                f"dropped an established fact from the generic prompt - {label} must be: {value}"
                if value else f"dropped an established fact from the generic prompt: {label}"
            )

    if ledger:
        evaluated, violations, pending = goal_ledger.evaluate(ledger, doc if isinstance(doc, dict) else None, prompt)
        result["goals"] = evaluated
        result["goal_violations"] = violations
        result["goals_pending"] = [goal["id"] for goal in pending]
        failures.extend(violations)

    result["shared_failures"] = failures
    if failures:
        result["repair_required"] = True
    return result


