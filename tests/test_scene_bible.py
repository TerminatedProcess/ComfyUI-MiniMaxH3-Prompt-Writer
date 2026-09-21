import unittest

from backend.scene_bible import (
    FIELDS,
    ORIGIN_ASSET,
    ORIGIN_INVENTED,
    ORIGIN_USER,
    SceneBibleError,
    apply_update,
    lock_violations,
    locked_fields,
    new_bible,
    render_constraints,
    set_field,
    validate,
)


def bible_with(**kwargs):
    b = new_bible()
    for key, (value, origin) in kwargs.items():
        b = set_field(b, key, value, origin)
    return b


class SetFieldTests(unittest.TestCase):
    def test_rejects_unknown_field(self):
        with self.assertRaises(SceneBibleError) as ctx:
            set_field(new_bible(), "vibe", "moody", ORIGIN_USER)
        self.assertEqual(ctx.exception.code, "UNKNOWN_FIELD")

    def test_rejects_unknown_origin(self):
        with self.assertRaises(SceneBibleError) as ctx:
            set_field(new_bible(), "subject", "Bob", "vibes")
        self.assertEqual(ctx.exception.code, "INVALID_ORIGIN")

    def test_does_not_mutate_input(self):
        original = bible_with(subject=("Bob", ORIGIN_USER))
        set_field(original, "location", "a kitchen", ORIGIN_INVENTED)
        self.assertNotIn("location", original["fields"])

    def test_strips_whitespace(self):
        b = set_field(new_bible(), "subject", "  Bob  ", ORIGIN_USER)
        self.assertEqual(b["fields"]["subject"], "Bob")


class ValidateTests(unittest.TestCase):
    def test_rejects_empty_value(self):
        b = new_bible()
        b["fields"]["subject"] = "   "
        b["origins"]["subject"] = ORIGIN_USER
        with self.assertRaises(SceneBibleError) as ctx:
            validate(b)
        self.assertEqual(ctx.exception.code, "EMPTY_FIELD")

    def test_rejects_origin_without_field(self):
        b = new_bible()
        b["origins"]["subject"] = ORIGIN_USER
        with self.assertRaises(SceneBibleError) as ctx:
            validate(b)
        self.assertEqual(ctx.exception.code, "ORPHAN_ORIGIN")

    def test_rejects_overlong_field(self):
        b = new_bible()
        b["fields"]["subject"] = "x" * 401
        b["origins"]["subject"] = ORIGIN_USER
        with self.assertRaises(SceneBibleError) as ctx:
            validate(b)
        self.assertEqual(ctx.exception.code, "FIELD_TOO_LONG")


class ApplyUpdateTests(unittest.TestCase):
    def test_updates_unlocked_field(self):
        b = bible_with(era=("1990s", ORIGIN_INVENTED))
        b, changed, overridden = apply_update(b, {"era": "1970s"})
        self.assertEqual(b["fields"]["era"], "1970s")
        self.assertEqual(changed, ("era",))
        self.assertEqual(overridden, ())

    def test_asset_locked_field_resists_silent_change(self):
        b = bible_with(subject=("a man in a red jacket", ORIGIN_ASSET))
        b, changed, overridden = apply_update(b, {"subject": "a woman in a blue coat"})
        self.assertEqual(b["fields"]["subject"], "a man in a red jacket")
        self.assertEqual(changed, ())
        self.assertEqual(overridden, ())

    def test_explicit_target_overrides_asset_lock(self):
        b = bible_with(subject=("a man in a red jacket", ORIGIN_ASSET))
        b, changed, overridden = apply_update(
            b, {"subject": "a man in a 70s suit"}, explicit_targets=("subject",)
        )
        self.assertEqual(b["fields"]["subject"], "a man in a 70s suit")
        self.assertEqual(changed, ("subject",))
        self.assertEqual(overridden, ("subject",), "an overridden lock must be reported to the UI")

    def test_locked_field_does_not_abort_sibling_edits(self):
        b = bible_with(
            subject=("a man in a red jacket", ORIGIN_ASSET),
            era=("1990s", ORIGIN_INVENTED),
        )
        b, changed, _ = apply_update(b, {"subject": "someone else", "era": "1970s"})
        self.assertEqual(changed, ("era",))
        self.assertEqual(b["fields"]["subject"], "a man in a red jacket")

    def test_noop_when_value_unchanged(self):
        b = bible_with(era=("1970s", ORIGIN_INVENTED))
        _, changed, _ = apply_update(b, {"era": "1970s"})
        self.assertEqual(changed, ())

    def test_changed_order_follows_field_order(self):
        b = bible_with(
            camera=("static", ORIGIN_INVENTED),
            subject=("Bob", ORIGIN_INVENTED),
        )
        _, changed, _ = apply_update(b, {"camera": "push in", "subject": "Alice"})
        self.assertEqual(changed, ("subject", "camera"), "must not depend on dict ordering")

    def test_rejects_empty_update_value(self):
        b = bible_with(era=("1990s", ORIGIN_INVENTED))
        with self.assertRaises(SceneBibleError) as ctx:
            apply_update(b, {"era": "  "})
        self.assertEqual(ctx.exception.code, "EMPTY_FIELD")


class ConstraintRenderingTests(unittest.TestCase):
    def test_renders_in_field_order(self):
        b = bible_with(
            camera=("slow push in", ORIGIN_INVENTED),
            subject=("Bob", ORIGIN_USER),
            location=("his living room", ORIGIN_USER),
        )
        rendered = render_constraints(b)
        self.assertLess(rendered.index("subject"), rendered.index("location"))
        self.assertLess(rendered.index("location"), rendered.index("camera"))

    def test_marks_asset_fields_for_the_model(self):
        b = bible_with(subject=("a man in a red jacket", ORIGIN_ASSET))
        self.assertIn("reference image", render_constraints(b))

    def test_omits_unset_fields(self):
        b = bible_with(subject=("Bob", ORIGIN_USER))
        self.assertNotIn("weather", render_constraints(b))


class LockViolationTests(unittest.TestCase):
    def test_detects_dropped_subject(self):
        b = bible_with(subject=("Bob, wearing a faded denim jacket", ORIGIN_ASSET))
        prose = "A woman in a silk gown stands beside a window in the evening light."
        self.assertEqual(lock_violations(b, prose), ("subject",))

    def test_passes_when_facts_survive_reworded(self):
        b = bible_with(subject=("Bob, wearing a faded denim jacket", ORIGIN_ASSET))
        prose = ("The camera settles on Bob, whose faded denim jacket hangs open "
                 "as he leans back into the cushions.")
        self.assertEqual(lock_violations(b, prose), ())

    def test_ignores_unlocked_fields(self):
        b = bible_with(era=("1970s", ORIGIN_INVENTED))
        self.assertEqual(lock_violations(b, "A bare white room."), ())

    def test_dropped_name_fails_even_when_description_survives(self):
        # Observed in integration: the expander rendered a locked subject as
        # "an adult male in his late 30s" -- every visual fact intact, the name
        # gone. A ratio check passed it because the name was 1 token in 9.
        b = bible_with(
            subject=("Bob, a man in his late 30s, unkempt brown hair, faded denim jacket",
                     ORIGIN_ASSET),
        )
        prose = ("An adult male in his late 30s with unkempt brown hair sits slumped, "
                 "his faded denim jacket open at the collar.")
        self.assertEqual(lock_violations(b, prose), ("subject",))

    def test_name_present_passes(self):
        b = bible_with(
            subject=("Bob, a man in his late 30s, unkempt brown hair, faded denim jacket",
                     ORIGIN_ASSET),
        )
        prose = ("Bob, in his late 30s, sits slumped with unkempt brown hair and a "
                 "faded denim jacket open at the collar.")
        self.assertEqual(lock_violations(b, prose), ())

    def test_field_without_proper_noun_still_uses_ratio(self):
        b = bible_with(location=("a cramped basement workshop", ORIGIN_ASSET))
        self.assertEqual(lock_violations(b, "A cramped basement workshop lit by one bulb."), ())
        self.assertEqual(lock_violations(b, "A sunlit meadow stretches away."), ("location",))

    def test_stopwords_alone_do_not_satisfy_a_lock(self):
        b = bible_with(subject=("a man with a red umbrella", ORIGIN_ASSET))
        prose = "A person is in the room with the thing and the other."
        self.assertEqual(lock_violations(b, prose), ("subject",))


class LockedFieldsTests(unittest.TestCase):
    def test_returns_only_asset_fields_in_order(self):
        b = bible_with(
            camera=("static", ORIGIN_ASSET),
            subject=("Bob", ORIGIN_ASSET),
            era=("1970s", ORIGIN_INVENTED),
        )
        self.assertEqual(locked_fields(b), ("subject", "camera"))


class FieldsContractTests(unittest.TestCase):
    def test_fields_are_unique_and_ordered(self):
        self.assertEqual(len(FIELDS), len(set(FIELDS)))
        self.assertEqual(FIELDS[0], "subject", "subject leads; it is the most drift-prone fact")


if __name__ == "__main__":
    unittest.main()
