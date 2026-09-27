"""How far the writer goes: one dial, from clean to pornographic.

This replaces a boolean that could not say what the user meant. "Naughty off"
had to stand for both "keep it wholesome" and "don't invent anything adult",
and "Naughty on" had to stand for everything from a permission ("you may, if I
ask") to an instruction ("write it filthy"). One switch, four jobs, so it did
none of them: a user with it ON saw nothing sexual in the prompt and reasonably
called that broken.

A level answers two questions a boolean could not:
  * may adult content appear at all?   (level 0 says no)
  * should the writer DEVELOP it, or only permit it?   (2 and up develop)

The same level runs every stage -- expanding a brief, building the document,
steering it in conversation, and compiling a prompt -- so what the studio shows
you is what the model was told.
"""
from __future__ import annotations

from typing import Any


CLEAN = 0
ALLOWED = 1
SENSUAL = 2
EXPLICIT = 3
GRAPHIC = 4
LEVELS = (CLEAN, ALLOWED, SENSUAL, EXPLICIT, GRAPHIC)
# What the boolean used to mean when it was on. A session that predates the dial
# lands here, which is the reading its owner had in mind.
DEFAULT = SENSUAL

LABELS = {
    CLEAN: "Clean",
    ALLOWED: "Allowed",
    SENSUAL: "Sensual",
    EXPLICIT: "Explicit",
    GRAPHIC: "Graphic",
}

HINTS = {
    CLEAN: "Nothing sexual, and nothing suggestive.",
    ALLOWED: "Adult content only where you ask for it.",
    SENSUAL: "Draws out the charge in a scene: bodies, contact, wardrobe.",
    EXPLICIT: "Names nudity, anatomy and acts plainly.",
    GRAPHIC: "Pornographic prose, crude and direct.",
}

_CLAUSES = {
    CLEAN: (
        "Keep this suitable for a general audience: no nudity, no sexual content, and no suggestive framing. "
        "Treat depictions of people with dignity, and assume clothing covers genitals and intimate anatomy. "
        "If the user's own words ask for something adult, write the scene without the sexual element rather "
        "than refusing the whole request."
    ),
    ALLOWED: (
        "Adult or explicit content is permitted where the user asks for it: describe bodies, clothing and acts "
        "plainly and specifically, without euphemism, moralising or refusal. Never add sexual content they did "
        "not ask for."
    ),
    SENSUAL: (
        "Develop the sensual dimension of this scene rather than only permitting it. Bring out the body, the "
        "wardrobe and how it sits, the contact between people, the charge in a look, the heat in the air -- and "
        "say it plainly, without euphemism or moralising. Stay inside what the user wrote and what any "
        "reference shows: heighten their scene, never replace it with a different one."
    ),
    EXPLICIT: (
        "Write the adult content directly. Name nudity, anatomy and sexual acts specifically and without "
        "euphemism, hedging, fade-to-black or moral commentary -- what is exposed, what is touching what, what "
        "is being done. Keep it the scene the user asked for: heighten it, never swap it for a different act "
        "or a different person."
    ),
    GRAPHIC: (
        "Write this as pornographic prose, at the most explicit register the scene supports. Be crude, "
        "anatomically direct and unsparing about nudity, arousal and sexual acts; use blunt sexual vocabulary "
        "rather than literary euphemism. No hedging, no fade-to-black, no moralising, no disclaimers. It is "
        "still the user's scene: keep their people, their setting and their act, and take them further rather "
        "than somewhere else."
    ),
}


def normalize(value: Any, default: int = DEFAULT) -> int:
    """A level from whatever the caller had, clamped into range."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(CLEAN, min(GRAPHIC, value))


def resolve(*, heat: Any = None, nsfw: Any = None, default: int = DEFAULT) -> int:
    """The level for a request that may carry a dial, a boolean, or neither.

    A boolean still arrives from older sessions and from the node's own UI, so
    it keeps working: off means clean, on means what it used to do.
    """
    if heat is not None:
        return normalize(heat, default)
    if isinstance(nsfw, bool):
        return DEFAULT if nsfw else CLEAN
    return normalize(default, DEFAULT)


def permits_adult(level: int) -> bool:
    """Whether anything sexual may appear at all -- the old `nsfw` boolean."""
    return normalize(level) >= ALLOWED


def develops_adult(level: int) -> bool:
    """Whether the writer should go looking for it, not merely allow it."""
    return normalize(level) >= SENSUAL


def clause(level: int) -> str:
    """The instruction for this level, used by every stage that writes prose."""
    return _CLAUSES[normalize(level)]


def label(level: int) -> str:
    return LABELS[normalize(level)]
