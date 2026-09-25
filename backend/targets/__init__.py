"""The target registry: one owner per mode, and one place the UI reads.

Everything that used to be a hardcoded mode list -- in routes, media, context,
intent, and three separate frontend modules -- resolves through here instead.
That matters less for correctness than for drift: six duplicate mode lists is
how "Krea 2 works but its Settings card is missing" happens.

Adding a model is a spec module plus a vendored guide. It is not a new branch in
the pipeline, the route handlers, or the studio markup.
"""
from __future__ import annotations

from typing import Any

from ..guides import load_guide
from .anima import ANIMA
from .base import Mode, Target, TargetError, compose, shared_checks, strip_fence
from .h3 import H3, MUSIC3
from .krea2 import KREA2

TARGETS: tuple[Target, ...] = (H3, KREA2, ANIMA, MUSIC3)
BY_ID = {target.id: target for target in TARGETS}
# mode id -> (target, mode). Modes are globally unique; the registry asserts it.
_MODE_INDEX: dict[str, tuple[Target, Mode]] = {}
for _target in TARGETS:
    for _mode in _target.modes:
        if _mode.id in _MODE_INDEX:
            raise TargetError("DUPLICATE_MODE", f"Mode {_mode.id!r} is claimed by two targets.")
        _MODE_INDEX[_mode.id] = (_target, _mode)

# The workspace order the studio shows, left to right.
WORKSPACES = ("image", "video", "music")
DEFAULT_MODE = "Krea2"


def all_modes() -> tuple[str, ...]:
    return tuple(_MODE_INDEX)


def generation_modes() -> tuple[str, ...]:
    return tuple(mode for mode, (_target, spec) in _MODE_INDEX.items() if spec.generation)




def target_for_mode(mode: str) -> Target:
    try:
        return _MODE_INDEX[mode][0]
    except KeyError:
        raise TargetError("INVALID_MODE", "The selected generation mode is not supported.") from None


def mode_spec(mode: str) -> Mode:
    try:
        return _MODE_INDEX[mode][1]
    except KeyError:
        raise TargetError("INVALID_MODE", "The selected generation mode is not supported.") from None


def target_by_id(target_id: str) -> Target:
    try:
        return BY_ID[target_id]
    except KeyError:
        raise TargetError("INVALID_TARGET", "The selected target model is not supported.") from None


def mode_limits() -> dict[str, dict[str, int]]:
    """Reference limits per mode, in the shape the media store enforces."""
    return {mode: dict(spec.reference_limits) for mode, (_target, spec) in _MODE_INDEX.items() if spec.generation}


def profile_for_mode(mode: str) -> str:
    return mode_spec(mode).profile




def profiles() -> tuple[dict[str, Any], ...]:
    """System-prompt profiles, for the Settings cards."""
    seen: dict[str, dict[str, Any]] = {}
    for mode, (target, spec) in _MODE_INDEX.items():
        entry = seen.setdefault(spec.profile, {
            "id": spec.profile,
            "target": target.id,
            "label": target.label,
            "modes": [],
        })
        entry["modes"].append(mode)
    for entry in seen.values():
        entry["description"] = "Instructions used by " + ", ".join(entry["modes"]) + "."
    return tuple(seen.values())


def system_prompt_for(
    mode: str,
    *,
    nsfw: bool = True,
    story: bool = True,
    variant: str | None = None,
) -> str:
    target = target_for_mode(mode)
    return target.system_prompt(mode, nsfw=nsfw, story=story, variant=variant)


def guide_ids_for_mode(mode: str) -> tuple[str, ...]:
    return mode_spec(mode).guide_ids


def catalog() -> list[dict[str, Any]]:
    """The whole registry as JSON -- the studio builds its UI from this.

    Includes each guide's title and source URL so the studio's workspaces, model
    picker, field visibility and variant picker come from one description. The
    Settings cards still list their profiles by hand -- `profiles()` is the shape
    they should read, and wiring them is the next cut of this.
    """
    result = []
    for target in TARGETS:
        result.append({
            "id": target.id,
            "label": target.label,
            "vendor": target.vendor,
            "workspace": target.workspace,
            "media_kind": target.media_kind,
            "output_shape": target.output_shape,
            "brief_limit": target.brief_limit,
            "fields": sorted(target.fields),
            "variants": list(target.variants),
            "default_variant": target.default_variant,
            "supports_sequence": target.supports_sequence,
            "infers_mode": target.infer_mode is not None,
            "modes": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "summary": spec.summary,
                    "profile": spec.profile,
                    "generation": spec.generation,
                    "reference_limits": dict(spec.reference_limits),
                    "guides": [
                        {
                            "id": guide_id,
                            "title": load_guide(guide_id)["title"],
                            "source_url": load_guide(guide_id)["source_url"],
                            "vendor": load_guide(guide_id)["vendor"],
                        }
                        for guide_id in spec.guide_ids
                    ],
                }
                for spec in target.modes
            ],
        })
    return result


__all__ = [
    "ANIMA",
    "BY_ID",
    "DEFAULT_MODE",
    "H3",
    "KREA2",
    "MUSIC3",
    "Mode",
    "Target",
    "TargetError",
    "TARGETS",
    "WORKSPACES",
    "all_modes",
    "catalog",
    "compose",
    "generation_modes",
    "guide_ids_for_mode",
    "mode_limits",
    "mode_spec",
    "profile_for_mode",
    "profiles",
    "shared_checks",
    "strip_fence",
    "system_prompt_for",
    "target_by_id",
    "target_for_mode",
]
