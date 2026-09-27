"""Human handles for the people in a document.

"B's wardrobe" is a database row; "Bob's tights" is a sentence. The letters are
still the machine's handle -- relations use them, and the audit reports by them
-- but everything a person reads or says is easier with a name, and the model
writes noticeably more natural prose about Liz and Bob than about A and B.

A name never reaches the finished prompt. Krea 2 and H3 have never met Liz: the
name carries no visual information, and a recognisable one drags the render
toward whoever the model thinks it belongs to. Names are resolved back into
descriptions at compile time, which is the same rule the letters follow.

The lists are deliberately unremarkable -- no celebrities, no names that carry a
strong period or character association -- because the point is a handle, not a
suggestion about who the person is.
"""
from __future__ import annotations

from hashlib import blake2b

FEMININE = (
    "Liz", "Nora", "Ruth", "Jane", "Clara", "Edith", "Maud", "Alice", "Sylvie", "Greta",
    "Iris", "Paula", "Rosa", "Tess", "Vera", "Wendy", "Dora", "Nell", "Mabel", "Ines",
)
MASCULINE = (
    "Bob", "Carl", "Ned", "Hugo", "Otto", "Ralph", "Victor", "Wes", "Gus", "Milo",
    "Clyde", "Dean", "Emil", "Frank", "Hal", "Ivan", "Leo", "Roy", "Stan", "Walt",
)
NEUTRAL = (
    "Alex", "Robin", "Sam", "Jude", "Quinn", "Reese", "Rowan", "Sasha", "Blake", "Morgan",
)

_FEMININE_CUES = (
    "woman", "women", "girl", "lady", "female", "she", "her", "hers", "mother", "sister",
    "daughter", "bride", "waitress", "actress", "queen", "wife", "mrs", "ms",
)
_MASCULINE_CUES = (
    "man", "men", "boy", "gentleman", "male", "he", "his", "him", "father", "brother",
    "son", "groom", "waiter", "king", "husband", "mr",
)


def _words(text: str) -> set[str]:
    return {word.strip(".,;:'\"()").lower() for word in (text or "").split()}


def pool(description: str) -> tuple[str, ...]:
    """Which list to draw from, read off how the person was described.

    Neutral when the description does not say, because a wrong name is stickier
    than a wrong letter: the user would be calling her Bob for the rest of the
    session. "Woman" beats "man" on a tie only because "a woman with a man's
    watch" is about her.
    """
    words = _words(description)
    feminine = bool(words & set(_FEMININE_CUES))
    masculine = bool(words & set(_MASCULINE_CUES))
    if feminine and not masculine:
        return FEMININE
    if masculine and not feminine:
        return MASCULINE
    if feminine and masculine:
        return FEMININE
    return NEUTRAL


def choose(description: str, *, seed: str, index: int, taken: set[str] | None = None) -> str:
    """A stable name for this person. Same session, same person, same name.

    Deterministic on purpose: a name that changed on every rebuild would make
    the conversation history lie -- "make Liz's dress red" has to keep pointing
    at the same body.
    """
    used = {name.lower() for name in (taken or set())}
    candidates = pool(description)
    digest = blake2b(f"{seed}:{index}".encode(), digest_size=8).digest()
    start = int.from_bytes(digest, "big") % len(candidates)
    for offset in range(len(candidates)):
        name = candidates[(start + offset) % len(candidates)]
        if name.lower() not in used:
            return name
    # Every name in the pool is taken (six people, twenty names -- unreachable
    # in practice, but a document must never end up with two of the same).
    for fallback in (*NEUTRAL, *FEMININE, *MASCULINE):
        if fallback.lower() not in used:
            return fallback
    return f"Person {index}"
