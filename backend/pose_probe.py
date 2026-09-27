"""Reading a body's pose off a reference image, without asking for the pose.

Measured, not assumed. On a real reference (a woman at a desk, wide-angle selfie)
qwen3-vl-27b answered "sitting" when asked whether she was standing or sitting --
and answered it again when the choice was constrained to a menu, reasoning itself
there from her torso height against the desk. Asked instead what was *visible*, the
same model on the same image said: the chair seat is not occupied, her lap is not
visible, her body is in front of the desk edge. All three correct, and "standing"
follows from them by arithmetic.

So the contract here is: the model reports evidence, this module draws the
conclusion, and when the evidence does not agree the pose is left unset rather
than guessed. A confidently wrong locked fact costs more than an empty field --
the empty one gets filled by the next conversation turn, the wrong one has to be
noticed first.
"""
from __future__ import annotations

from typing import Any

# What the probe asks for. Every key is something you can point at in the
# picture; none of them name a pose.
QUESTIONS = {
    "people_count": "how many people are visible -- exactly one of: none, one, more than one",
    "seat_in_frame": "is any chair, stool, bench or seat visible in the picture",
    "seat_occupied": "is a person's weight resting on that seat -- false if the seat is empty",
    "lap_visible": "is the person's lap or the top of their thighs visible as a horizontal surface",
    "legs_upright": "are the person's legs visible and vertical, carrying their weight",
    "body_vs_furniture": (
        "where the person's body is relative to the nearest desk, table or counter -- exactly one of: "
        "behind it, in front of its edge, beside it, none visible"
    ),
    "camera_height": "exactly one of: above eye level, eye level, below eye level",
}

BOOLEAN_KEYS = ("seat_in_frame", "seat_occupied", "lap_visible", "legs_upright")
BODY_CHOICES = ("behind it", "in front of its edge", "beside it", "none visible")
CAMERA_CHOICES = ("above eye level", "eye level", "below eye level")
COUNT_CHOICES = ("none", "one", "more than one")
MANY = "more than one"


def schema_line() -> str:
    """The JSON shape the model must answer with."""
    booleans = ", ".join(f'"{key}": true/false' for key in BOOLEAN_KEYS)
    return (
        '{"people_count": "<one of: ' + " | ".join(COUNT_CHOICES) + '>", ' + booleans
        + ', "body_vs_furniture": "<one of: ' + " | ".join(BODY_CHOICES) + '>"'
        ', "camera_height": "<one of: ' + " | ".join(CAMERA_CHOICES) + '>"}'
    )


def normalize(raw: Any) -> dict[str, Any]:
    """Keep only answers in the shape asked for. An unparseable key is absent."""
    if not isinstance(raw, dict):
        return {}
    answers: dict[str, Any] = {}
    for key in BOOLEAN_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            answers[key] = value
        elif isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no"}:
            answers[key] = value.strip().lower() in {"true", "yes"}
    for key, choices in (
        ("people_count", COUNT_CHOICES),
        ("body_vs_furniture", BODY_CHOICES),
        ("camera_height", CAMERA_CHOICES),
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip().lower() in choices:
            answers[key] = value.strip().lower()
    return answers


def derive(answers: dict[str, Any]) -> tuple[str | None, str]:
    """(pose phrase, why) from the evidence -- or (None, why) when it disagrees.

    Seated evidence beats standing evidence, because an occupied seat or a
    visible lap is a positive observation, while "standing" is mostly inferred
    from the absence of one.
    """
    answers = normalize(answers)
    if not answers:
        return None, "the picture was not readable"
    # With two bodies in frame the evidence stops being about one of them: the
    # seat can be occupied by one person while another stands beside it, and
    # every answer below would then describe whichever body the model happened
    # to look at. The document holds ONE subject, so there is no honest way to
    # pick -- say so instead of locking in a coin flip.
    if answers.get("people_count") == MANY:
        return None, "there is more than one person in the picture, so no single pose can be read from it"
    if answers.get("people_count") == "none":
        return None, "there is no person in the picture"

    seated = [
        "they are on the seat" if answers.get("seat_occupied") else "",
        "their lap is visible" if answers.get("lap_visible") else "",
    ]
    seated = [reason for reason in seated if reason]
    if seated:
        return "sitting", " and ".join(seated)

    standing = []
    if answers.get("legs_upright"):
        standing.append("their legs are upright and carrying them")
    if answers.get("seat_in_frame") and answers.get("seat_occupied") is False:
        standing.append("the seat in the picture is empty")
    if answers.get("body_vs_furniture") == "in front of its edge":
        standing.append("they are in front of the furniture rather than tucked into it")
    if standing:
        return "standing", " and ".join(standing)

    return None, "nothing in the picture settles whether they are standing or sitting"


POSES = ("standing", "sitting", "perched", "leaning", "kneeling", "crouching", "lying", "walking")
# Close enough not to be worth a question: a perched or leaning subject is
# neither standing nor sitting outright, and arguing about it wastes the user's
# attention on the one case where both readings are defensible.
_NEIGHBOURS = {"sitting": {"sitting", "perched"}, "standing": {"standing", "leaning", "walking"}}


def agrees(pose: str, written: str) -> bool:
    """Whether a pose the build already wrote says the same thing as the probe.

    Word-level on purpose: the build writes "Standing, three-quarter to the
    camera, one arm extended" and the probe derives "standing" -- the same fact
    in different clothes, and re-writing it would only churn the field.
    """
    said = {word for word in POSES if word in (written or "").lower()}
    if not said:
        return False
    return bool(said & _NEIGHBOURS.get(pose, {pose}))


# Said the way a person would, not the way the probe asked.
_PLACEMENT = {
    "behind it": "behind the desk",
    "in front of its edge": "in front of the desk edge",
    "beside it": "beside the desk",
}


def phrase(pose: str, answers: dict[str, Any]) -> str:
    """The pose field's value: the body first, then what the camera sees."""
    answers = normalize(answers)
    parts = [pose]
    if pose == "sitting" and answers.get("seat_occupied"):
        parts.append("on the seat")
    placement = _PLACEMENT.get(answers.get("body_vs_furniture", ""))
    if placement:
        parts.append(placement)
    height = answers.get("camera_height")
    if height and height != "eye level":
        parts.append(f"camera {height}")
    return ", ".join(parts)
