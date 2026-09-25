"""The generic prompt document: origins, the three correction cases, and locks.

The three cases are the ones that motivated `override` as a distinct origin:

    A  the image shows red, the document never said anything  -> a gap, filled
       by re-observing (origin asset)
    B  the image shows blue, the user wants red anyway         -> a deliberate
       deviation that must never be reverted (origin override)
    C  the image shows red, the document wrongly says blue     -> corrected by
       re-observing, or by the user's own words (origin user)

Without B, cases A and C are indistinguishable from B, and a later
re-observation silently undoes the user's choice.
"""
from __future__ import annotations

import unittest

from backend import generic


def doc_with(**fields):
    doc = generic.new_doc()
    for key, (value, origin) in fields.items():
        doc = generic.set_field(doc, key, value, origin)
    return doc


class DocumentTests(unittest.TestCase):
    def test_absent_field_reads_as_unspecified(self):
        record = generic.record(generic.new_doc(), "wardrobe")
        self.assertEqual(record, {"value": "", "origin": "unspecified", "observed": None})

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(generic.GenericError):
            generic.set_field(generic.new_doc(), "vibes", "nice", generic.ORIGIN_USER)

    def test_validate_rejects_an_origin_without_a_value(self):
        with self.assertRaises(generic.GenericError):
            generic.validate({"schema": "generic/1", "fields": {"subject": {"value": "", "origin": "user"}}})

    def test_render_groups_fields_under_headings(self):
        doc = doc_with(
            subject=("Bob, a man in his late 30s", generic.ORIGIN_USER),
            location=("his living room", generic.ORIGIN_USER),
        )
        rendered = generic.render(doc)
        self.assertIn("Subject\nSubject: Bob, a man in his late 30s", rendered)
        self.assertIn("Scene\nLocation: his living room", rendered)


class CaseAGapFilledByObservationTests(unittest.TestCase):
    """The prompt came out blue because nothing ever said red."""

    def test_unspecified_field_is_visible_as_a_gap(self):
        doc = doc_with(subject=("a woman on a bridge", generic.ORIGIN_USER))
        self.assertIn("wardrobe", generic.unspecified_fields(doc))

    def test_observation_fills_the_gap_as_an_asset_fact(self):
        doc = doc_with(subject=("a woman on a bridge", generic.ORIGIN_USER))
        doc, changed, protected = generic.apply_patch(
            doc, {"wardrobe": "a red pleated skirt and white blouse"}, source=generic.SOURCE_OBSERVE
        )
        self.assertEqual(changed, ("wardrobe",))
        self.assertEqual(protected, ())
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_ASSET)
        self.assertEqual(record["observed"], "a red pleated skirt and white blouse")

    def test_a_filled_gap_is_locked_into_every_compile(self):
        doc = doc_with(wardrobe=("a red pleated skirt", generic.ORIGIN_ASSET))
        self.assertIn("wardrobe", generic.locked_fields(doc))
        self.assertIn("- Wardrobe: a red pleated skirt", generic.render_constraints(doc))


class CaseBDeliberateOverrideTests(unittest.TestCase):
    """The image is blue, the user wants red, and that must stick."""

    def setUp(self):
        self.doc = doc_with(wardrobe=("a blue skirt", generic.ORIGIN_ASSET))

    def test_user_contradicting_the_image_is_recorded_as_an_override(self):
        doc, changed, _protected = generic.apply_patch(
            self.doc, {"wardrobe": "a red skirt"}, source=generic.SOURCE_USER
        )
        self.assertEqual(changed, ("wardrobe",))
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_OVERRIDE)
        self.assertEqual(record["value"], "a red skirt")
        self.assertEqual(record["observed"], "a blue skirt")

    def test_a_later_re_observation_does_not_revert_the_override(self):
        doc, _changed, _protected = generic.apply_patch(
            self.doc, {"wardrobe": "a red skirt"}, source=generic.SOURCE_USER
        )
        doc, changed, protected = generic.apply_patch(
            doc, {"wardrobe": "a blue skirt"}, source=generic.SOURCE_OBSERVE
        )
        self.assertEqual(changed, ())
        self.assertEqual(protected, ("wardrobe",))
        self.assertEqual(generic.record(doc, "wardrobe")["value"], "a red skirt")

    def test_the_override_is_explained_to_the_compile_step(self):
        doc, _changed, _protected = generic.apply_patch(
            self.doc, {"wardrobe": "a red skirt"}, source=generic.SOURCE_USER
        )
        constraints = generic.render_constraints(doc)
        self.assertIn("the user's deliberate choice", constraints)
        self.assertIn("a blue skirt", constraints)


class CaseCCorrectionTests(unittest.TestCase):
    def test_user_correcting_an_invented_value_becomes_their_words(self):
        doc = doc_with(wardrobe=("a yellow coat", generic.ORIGIN_INVENTED))
        doc, changed, _protected = generic.apply_patch(
            doc, {"wardrobe": "a red skirt"}, source=generic.SOURCE_USER
        )
        self.assertEqual(changed, ("wardrobe",))
        self.assertEqual(generic.record(doc, "wardrobe")["origin"], generic.ORIGIN_USER)

    def test_re_observation_corrects_a_wrong_asset_value(self):
        doc = doc_with(wardrobe=("a blue skirt", generic.ORIGIN_ASSET))
        doc, changed, _protected = generic.apply_patch(
            doc, {"wardrobe": "a red skirt"}, source=generic.SOURCE_OBSERVE
        )
        self.assertEqual(changed, ("wardrobe",))
        self.assertEqual(generic.record(doc, "wardrobe")["origin"], generic.ORIGIN_ASSET)

    def test_re_observation_leaves_the_users_own_words_alone(self):
        doc = doc_with(wardrobe=("a red skirt", generic.ORIGIN_USER))
        doc, changed, protected = generic.apply_patch(
            doc, {"wardrobe": "a blue skirt"}, source=generic.SOURCE_OBSERVE
        )
        self.assertEqual((changed, protected), ((), ("wardrobe",)))


class StoryBuilderTests(unittest.TestCase):
    def test_invention_only_fills_gaps(self):
        doc = doc_with(
            wardrobe=("a red skirt", generic.ORIGIN_ASSET),
            subject=("Bob", generic.ORIGIN_USER),
        )
        doc, changed, _protected = generic.apply_patch(
            doc,
            {"wardrobe": "a green tracksuit", "lighting": "low winter sun through net curtains"},
            source=generic.SOURCE_INVENT,
        )
        self.assertEqual(changed, ("lighting",))
        self.assertEqual(generic.record(doc, "wardrobe")["value"], "a red skirt")
        self.assertEqual(generic.record(doc, "lighting")["origin"], generic.ORIGIN_INVENTED)

    def test_invented_facts_are_not_locked(self):
        doc = doc_with(lighting=("low winter sun", generic.ORIGIN_INVENTED))
        self.assertEqual(generic.locked_fields(doc), ())
        self.assertEqual(generic.lock_violations(doc, "a completely different prompt"), ())


class LockTests(unittest.TestCase):
    def test_a_dropped_locked_fact_is_detected(self):
        doc = doc_with(wardrobe=("a red pleated skirt", generic.ORIGIN_ASSET))
        self.assertEqual(generic.lock_violations(doc, "a woman stands on a bridge at dusk"), ("wardrobe",))

    def test_a_surviving_fact_passes_even_when_reworded(self):
        doc = doc_with(wardrobe=("a red pleated skirt", generic.ORIGIN_ASSET))
        prose = "she wears a pleated red skirt, hem lifting in the wind"
        self.assertEqual(generic.lock_violations(doc, prose), ())

    def test_a_dropped_name_fails_even_when_the_description_survives(self):
        doc = doc_with(subject=("Bob, a man in his late 30s in a denim jacket", generic.ORIGIN_USER))
        prose = "an adult male in his late 30s in a denim jacket looks out of the window"
        self.assertEqual(generic.lock_violations(doc, prose), ("subject",))

    def test_expected_values_are_quoted_for_the_repair_turn(self):
        doc = doc_with(wardrobe=("a red pleated skirt", generic.ORIGIN_ASSET))
        self.assertEqual(
            generic.lock_expected(doc, ("wardrobe",)),
            {"wardrobe": "a red pleated skirt"},
        )


if __name__ == "__main__":
    unittest.main()
