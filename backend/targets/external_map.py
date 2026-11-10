"""Another app's model base -> this writer's (mode, variant). The one place it lives.

Callers speak InvokeAI base ids (`sdxl`, `sd-1`, `krea-2`, `minimax-h3`, ...).
A base with no target here is refused with UNSUPPORTED_TARGET rather than
mapped to the nearest thing: a Flux prompt written to the Krea 2 contract looks
fine and renders wrong, which is worse than an honest "not yet".

`family` disambiguates SDXL fine-tunes InvokeAI files under plain `sdxl`:
Illustrious and Pony need opposite quality tags, and the only signal is the
CivitAI `baseModel` label ("Illustrious", "Pony"), matched as a substring.
"""
from __future__ import annotations

from .base import TargetError

_SDXL_FAMILIES = (("illustrious", "illustrious"), ("noobai", "illustrious"), ("pony", "pony"))
_ANIMA_VARIANTS = ("base", "aesthetic", "turbo")


def resolve(
    base: str, *, variant: str | None = None, family: str | None = None, image_count: int = 0,
) -> tuple[str, str | None]:
    key = str(base or "").strip().lower().replace("_", "-")
    hint = str(family or "").strip().lower()
    if key == "minimax-h3":
        # A handed-over still is a first frame; H3 picks its own mode from the
        # media count everywhere else, so it does here too.
        from .h3 import infer_mode

        return infer_mode(image_count), None
    if key in {"krea-2", "krea2"}:
        return "Krea2", None
    if key == "anima":
        wanted = str(variant or "").strip().lower()
        return "Anima", wanted if wanted in _ANIMA_VARIANTS else None
    if key == "sdxl":
        for needle, resolved in _SDXL_FAMILIES:
            if needle in hint:
                return "SDTags", resolved
        return "SDTags", "sdxl"
    if key in {"sd-1", "sd1", "sd-1.5"}:
        return "SDTags", "sd1"
    raise TargetError("UNSUPPORTED_TARGET", f"The writer has no prompt target for base {base!r} yet.")
