"""The additive repair pass: restoring a dropped fact without a rewrite.

The property every test here is defending is the same one: an insertion adds
text and changes nothing else. A pass that can also lose something is just the
rewrite loop with extra steps.
"""
from __future__ import annotations

import unittest

from backend import fact_insertion, generic
from backend.targets import h3, krea2


REFERENCE_DRAFT = (
    "subject_definitions: <Subject 1> is a woman in her 30s shown in <Picture 1>.\n\n"
    "summary: [Reference video generation] She dances in an apartment.\n\n"
    "retention_analysis: The performer and the room are retained from the references.\n\n"
    "detailed_description: [Shot 1] She dances in a bright apartment, hips swaying.\n\n"
    "overall_soundscape: Quiet room tone.\n\n"
    "non_diegetic_music: N/A"
)


def doc(**fields):
    document = generic.new_doc()
    for key, value in fields.items():
        document = generic.set_field(document, key, value, generic.ORIGIN_USER)
    return document


def assembled(document, mode="Reference"):
    return {"input": {"mode": mode, "generic": document, "duration_seconds": 10, "goals": []}}


class AdditiveKeyTests(unittest.TestCase):
    def test_a_dropped_fact_is_additive(self):
        document = doc(era="Contemporary", time_of_day="Late afternoon")
        audit = h3.audit(REFERENCE_DRAFT, assembled(document))
        self.assertEqual(
            fact_insertion.additive_keys(audit, document), ("era", "time_of_day")
        )

    def test_dialogue_is_left_to_the_rewrite_loop(self):
        """A spoken line needs <d> tags and a speaker ID to be legal at all.

        Inserting it as prose would swap a dropped fact for a format failure,
        which is not an improvement by any measure the audit has.
        """
        document = doc(dialogue="I am not leaving without it")
        audit = h3.audit(REFERENCE_DRAFT, assembled(document))
        self.assertIn("dialogue", audit["lock_violations"])
        self.assertEqual(fact_insertion.additive_keys(audit, document), ())

    def test_a_scene_bible_audit_is_not_treated_as_a_document(self):
        """Without a generic document the lock keys address a different field set."""
        audit = {"lock_violations": ["subject"]}
        self.assertEqual(fact_insertion.additive_keys(audit, None), ())


class PatchParsingTests(unittest.TestCase):
    def test_key_and_sentence_lines_are_read(self):
        text = "era | The era is contemporary.\n- time_of_day | It is late afternoon.\nchatter"
        self.assertEqual(
            fact_insertion.parse_patch(text, ("era", "time_of_day")),
            {"era": "The era is contemporary.", "time_of_day": "It is late afternoon."},
        )

    def test_a_key_that_was_not_asked_for_is_ignored(self):
        self.assertEqual(fact_insertion.parse_patch("mood | Tense.", ("era",)), {})

    def test_a_prompt_the_model_returned_instead_of_a_patch_yields_nothing(self):
        self.assertEqual(fact_insertion.parse_patch(REFERENCE_DRAFT, ("era",)), {})


class SafetyTests(unittest.TestCase):
    def test_a_sentence_inventing_a_reference_tag_is_refused(self):
        self.assertFalse(fact_insertion._safe("She stands beside <Picture 7>.", REFERENCE_DRAFT))

    def test_a_subject_handle_the_draft_already_defines_is_allowed(self):
        self.assertTrue(fact_insertion._safe("<Subject 1> is standing.", REFERENCE_DRAFT))

    def test_a_subject_handle_the_draft_never_defined_is_refused(self):
        self.assertFalse(fact_insertion._safe("<Subject 4> is standing.", REFERENCE_DRAFT))

    def test_a_timestamp_is_refused(self):
        """Timestamps are audited against the clip length, so prose must not carry one."""
        self.assertFalse(fact_insertion._safe("At 00:04 she turns.", REFERENCE_DRAFT))

    def test_the_literal_sentences_are_themselves_safe(self):
        document = doc(era="Contemporary", pose="Standing, hands raised")
        for key in ("era", "pose"):
            sentence = fact_insertion.literal_sentence(document, key, REFERENCE_DRAFT)
            self.assertTrue(fact_insertion._safe(sentence, REFERENCE_DRAFT), sentence)


class LiteralSentenceTests(unittest.TestCase):
    def test_a_person_fact_uses_the_handle_the_draft_defines(self):
        document = doc(subject="A woman in her 30s", pose="Standing, angled slightly to the left")
        self.assertEqual(
            fact_insertion.literal_sentence(document, "pose", REFERENCE_DRAFT),
            "<Subject 1> is standing, angled slightly to the left.",
        )

    def test_a_draft_with_no_handle_describes_the_person_instead(self):
        """Krea 2 and non-Reference H3 have no <Subject N>, and must not grow one."""
        document = doc(subject="A woman in her 30s", wardrobe="A red pleated skirt")
        self.assertEqual(
            fact_insertion.literal_sentence(document, "wardrobe", "A quiet portrait."),
            "The woman in her 30s is wearing a red pleated skirt.",
        )

    def test_the_subject_field_does_not_describe_itself_twice(self):
        document = doc(subject="A woman in her 30s")
        self.assertEqual(
            fact_insertion.literal_sentence(document, "subject", "A quiet portrait."),
            "The subject is a woman in her 30s.",
        )

    def test_a_name_is_never_written_into_the_prompt(self):
        """A recognisable name drags the render toward whoever the model thinks it is."""
        document = generic.set_name(doc(subject="A woman in her 30s", pose="Standing"), 1, "Sylvie")
        self.assertNotIn("Sylvie", fact_insertion.literal_sentence(document, "pose", "A portrait."))

    def test_every_restorable_field_produces_a_sentence_that_clears_the_audit(self):
        """The backstop is only a backstop if it always works."""
        for field in generic.FIELDS:
            if field in fact_insertion.STRUCTURAL_FIELDS or field == "exclusions":
                continue
            value = "A deliberate ceramic vase of white ranunculus"
            sentence = fact_insertion.literal_sentence(doc(**{field: value}), field, "")
            self.assertTrue(sentence, field)
            self.assertTrue(generic.covers(value, sentence), f"{field}: {sentence}")


class SectionPlacementTests(unittest.TestCase):
    def test_a_wardrobe_fact_lands_in_subject_definitions(self):
        document = doc(wardrobe="A light blue ribbed knit dress")
        merged = h3.integrate_facts(
            assembled(document), REFERENCE_DRAFT, [("wardrobe", "She wears a light blue ribbed knit dress.")]
        )
        definitions = merged.split("summary:")[0]
        self.assertIn("light blue ribbed knit dress", definitions)

    def test_a_sound_fact_lands_in_the_soundscape_section(self):
        document = doc(soundscape="Ambient room tone with footsteps")
        merged = h3.integrate_facts(
            assembled(document), REFERENCE_DRAFT, [("soundscape", "Footsteps cross the room tone.")]
        )
        self.assertIn("Quiet room tone. Footsteps cross the room tone.", merged)

    def test_everything_outside_the_insertion_is_byte_identical(self):
        """The whole point: text that was not inserted was not touched."""
        document = doc(era="Contemporary")
        sentence = "The era is contemporary."
        merged = h3.integrate_facts(assembled(document), REFERENCE_DRAFT, [("era", sentence)])
        self.assertEqual(merged.replace(f" {sentence}", "", 1), REFERENCE_DRAFT)

    def test_a_prompt_with_no_sections_appends_instead_of_failing(self):
        """Only Reference mode emits the six headers; the rest are one block."""
        merged = h3.integrate_facts(assembled(doc(era="Contemporary"), mode="T2VA"), "A quiet shot.", [("era", "The era is contemporary.")])
        self.assertEqual(merged, "A quiet shot. The era is contemporary.")

    def test_prose_targets_append(self):
        merged = krea2.KREA2.integrate_facts(
            assembled(doc(mood="Tense"), mode="Krea2"), "A ceramic vase.", [("mood", "The mood is tense.")]
        )
        self.assertEqual(merged, "A ceramic vase. The mood is tense.")


class AcceptanceTests(unittest.TestCase):
    def test_an_insertion_that_recovers_nothing_is_refused(self):
        before = {"lock_violations": ["era"], "shared_failures": ["x"]}
        after = {"lock_violations": ["era"], "shared_failures": ["x"]}
        self.assertEqual(fact_insertion.accept(before, after)[0], False)

    def test_an_insertion_that_breaks_the_format_is_refused_whole(self):
        """Two facts back is not worth an invented reference, and severity cannot see one.

        `prompt_audit.severity` weighs dropped facts, unmet goals and the
        shared-failure count -- a new unexpected tag would register as an
        improvement without this check.
        """
        before = {"lock_violations": ["era", "mood"], "shared_failures": ["a", "b"], "unexpected_reference_tags": []}
        after = {"lock_violations": [], "shared_failures": [], "unexpected_reference_tags": ["<Picture 7>"]}
        accepted, reason = fact_insertion.accept(before, after)
        self.assertFalse(accepted)
        self.assertIn("unexpected reference tags", reason)

    def test_a_clean_recovery_is_accepted(self):
        before = {"lock_violations": ["era"], "shared_failures": ["a"]}
        after = {"lock_violations": [], "shared_failures": []}
        self.assertEqual(fact_insertion.accept(before, after), (True, None))


class CoverageBarTests(unittest.TestCase):
    def test_the_insertion_is_judged_by_the_same_rule_as_the_audit(self):
        value = "a red pleated skirt"
        document = doc(wardrobe=value)
        prose = "A quiet portrait. She is wearing a red pleated skirt."
        self.assertTrue(generic.covers(value, prose))
        self.assertEqual(generic.lock_violations(document, prose), ())
        self.assertFalse(generic.covers(value, "She is dressed for the weather."))


if __name__ == "__main__":
    unittest.main()
