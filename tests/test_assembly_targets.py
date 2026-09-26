"""Compiling the generic prompt: per-target requests, flags, goals and locks.

The load-bearing case is the last class: the same document and the same goal
compiled for three different targets must carry both into every one of them. A
goal that only reached the target it was set on is the bug this whole layer
exists to prevent.
"""
from __future__ import annotations

import unittest

from backend import generic, goals
from backend.assembly import AssemblyError, assemble_refinement, assemble_request

SESSION = "11111111-2222-4333-8444-555555555555"


def doc():
    document = generic.new_doc()
    document = generic.set_field(document, "subject", "Bob, a man in his late 30s", generic.ORIGIN_USER)
    document = generic.set_field(
        document, "wardrobe", "a red pleated skirt", generic.ORIGIN_OVERRIDE, observed="a blue skirt"
    )
    document = generic.set_field(document, "lighting", "low winter sun", generic.ORIGIN_INVENTED)
    return document


def body(mode, **overrides):
    payload = {
        "mode": mode,
        "session_id": SESSION,
        "creative_brief": "a quiet portrait at a tram stop",
    }
    if mode in {"T2VA", "I2VA", "FL2VA", "L2VA", "Reference"}:
        payload.update(duration_seconds=10, aspect_ratio="16:9")
    payload.update(overrides)
    return payload


def user_message(assembled):
    return next(message["content"] for message in assembled["messages"] if message["role"] == "user")


def system_names(assembled):
    return [message.get("name") for message in assembled["messages"] if message["role"] == "system"]


class ImageTargetAssemblyTests(unittest.TestCase):
    def test_krea2_assembles_without_a_duration(self):
        assembled = assemble_request(body("Krea2"))
        self.assertIsNone(assembled["input"]["duration_seconds"])
        self.assertIn("Target: Krea 2", user_message(assembled))

    def test_krea2_receives_both_official_krea_documents(self):
        assembled = assemble_request(body("Krea2"))
        self.assertEqual(
            system_names(assembled),
            [
                "prompt_studio_system_prompt",
                "official_krea2_prompting_guide",
                "official_krea2_expansion_instructions",
            ],
        )

    def test_anima_receives_its_model_card_rules(self):
        assembled = assemble_request(body("Anima"))
        self.assertIn("official_anima_prompting_rules", system_names(assembled))

    def test_anima_variant_defaults_and_is_reported(self):
        assembled = assemble_request(body("Anima"))
        self.assertEqual(assembled["input"]["variant"], "turbo")
        self.assertIn("Variant: turbo", user_message(assembled))
        chosen = assemble_request(body("Anima", variant="aesthetic"))
        self.assertEqual(chosen["input"]["variant"], "aesthetic")

    def test_an_unknown_variant_is_refused(self):
        with self.assertRaises(AssemblyError) as raised:
            assemble_request(body("Anima", variant="ultra"))
        self.assertEqual(raised.exception.code, "INVALID_VARIANT")

    def test_a_variant_on_a_target_without_variants_is_refused(self):
        with self.assertRaises(AssemblyError) as raised:
            assemble_request(body("Krea2", variant="turbo"))
        self.assertEqual(raised.exception.code, "INVALID_VARIANT")

    def test_an_image_target_needs_either_a_brief_or_a_document(self):
        with self.assertRaises(AssemblyError) as raised:
            assemble_request(body("Krea2", creative_brief=""))
        self.assertEqual(raised.exception.code, "INVALID_REQUEST")
        assembled = assemble_request(body("Krea2", creative_brief="", generic=doc()))
        self.assertIn("Generic prompt:", user_message(assembled))

    def test_aspect_ratio_is_optional_for_an_image_target(self):
        assembled = assemble_request(body("Krea2"))
        self.assertIsNone(assembled["input"]["aspect_ratio"])
        with_ratio = assemble_request(body("Krea2", aspect_ratio="3:2"))
        self.assertEqual(with_ratio["input"]["aspect_ratio"], "3:2")

    def test_a_bad_aspect_ratio_is_still_refused(self):
        with self.assertRaises(AssemblyError) as raised:
            assemble_request(body("Krea2", aspect_ratio="7:11"))
        self.assertEqual(raised.exception.code, "INVALID_ASPECT_RATIO")


class FlagTests(unittest.TestCase):
    def test_both_flags_default_on_in_a_request(self):
        assembled = assemble_request(body("T2VA"))
        self.assertTrue(assembled["input"]["nsfw"])
        self.assertTrue(assembled["input"]["story"])
        self.assertIn("Adult, explicit or otherwise mature content is permitted",
                      assembled["system_prompt"]["content"])

    def test_flags_off_restores_the_strict_contract(self):
        assembled = assemble_request(body("T2VA", nsfw=False, story=False))
        content = assembled["system_prompt"]["content"]
        self.assertIn("do not invent unsupported subject actions", content)
        self.assertNotIn("Adult, explicit", content)

    def test_a_non_boolean_flag_is_refused(self):
        for flag in ("story", "nsfw"):
            with self.assertRaises(AssemblyError):
                assemble_request(body("T2VA", **{flag: "yes"}))

    def test_a_system_prompt_override_still_wins_wholesale(self):
        assembled = assemble_request(body("Krea2", system_prompt_override="Write whatever you like."))
        self.assertEqual(assembled["system_prompt"]["content"], "Write whatever you like.")
        self.assertTrue(assembled["system_prompt"]["custom"])


class GenericAndGoalInjectionTests(unittest.TestCase):
    def test_established_facts_are_restated_for_every_compile(self):
        message = user_message(assemble_request(body("Krea2", generic=doc())))
        self.assertIn("Established facts", message)
        self.assertIn("Bob, a man in his late 30s", message)

    def test_an_override_explains_itself_so_it_is_not_corrected_back(self):
        message = user_message(assemble_request(body("Krea2", generic=doc())))
        self.assertIn("the user's deliberate choice", message)
        self.assertIn("a blue skirt", message)

    def test_invented_facts_are_carried_but_not_marked_as_established(self):
        message = user_message(assemble_request(body("Krea2", generic=doc())))
        self.assertIn("low winter sun", message)
        self.assertNotIn("low winter sun [", message)

    def test_a_malformed_document_is_refused_rather_than_ignored(self):
        with self.assertRaises(AssemblyError):
            assemble_request(body("Krea2", generic={"schema": "generic/1", "fields": {"nope": {"value": "x", "origin": "user"}}}))

    def test_goals_are_injected_as_standing_requirements(self):
        ledger = [goals.new_goal("always follow the clothing colours in the image", goals.KIND_FIELD, fields=("wardrobe",))]
        message = user_message(assemble_request(body("Krea2", goals=ledger)))
        self.assertIn("Standing goals", message)
        self.assertIn("always follow the clothing colours in the image", message)

    def test_a_paused_goal_is_not_injected(self):
        ledger = [goals.new_goal("never mention a brand")]
        ledger[0]["enabled"] = False
        message = user_message(assemble_request(body("Krea2", goals=ledger)))
        self.assertNotIn("never mention a brand", message)

    def test_a_document_only_compile_needs_no_brief_on_any_target(self):
        """The studio sends the document; demanding a brief too would reject it."""
        for mode in ("T2VA", "Reference", "Krea2", "Anima"):
            assembled = assemble_request(body(mode, creative_brief="", generic=doc()))
            message = user_message(assembled)
            self.assertIn("Bob, a man in his late 30s", message, mode)
            self.assertNotIn("Creative brief:", message, mode)

    def test_a_compile_with_neither_a_brief_nor_a_document_is_refused(self):
        for mode in ("T2VA", "Krea2"):
            with self.assertRaises(AssemblyError) as raised:
                assemble_request(body(mode, creative_brief=""))
            self.assertEqual(raised.exception.code, "INVALID_REQUEST", mode)

    def test_the_same_document_and_goal_reach_every_target(self):
        """The whole point: build once on the left, compile anywhere on the right."""
        ledger = [goals.new_goal("keep her skirt red", goals.KIND_PRESENCE, must_include=("red skirt",))]
        for mode in ("T2VA", "Reference", "Krea2", "Anima"):
            assembled = assemble_request(body(mode, generic=doc(), goals=ledger))
            message = user_message(assembled)
            self.assertIn("Bob, a man in his late 30s", message, mode)
            self.assertIn("keep her skirt red", message, mode)
            self.assertEqual(assembled["input"]["generic"]["fields"], doc()["fields"], mode)
            self.assertEqual(len(assembled["input"]["goals"]), 1, mode)


class RefinementTests(unittest.TestCase):
    """A revision is held to the same facts, goals and flags as the generation."""

    def refine(self, mode, **overrides):
        payload = {
            **body(mode),
            "current_prompt": "an existing prompt about a woman on a bridge",
            "instruction": "make the light colder",
            **overrides,
        }
        return assemble_refinement(payload, None)

    def test_an_image_target_is_not_refined_through_the_h3_path(self):
        assembled = self.refine("Krea2")
        message = user_message(assembled)
        self.assertIn("Revise the current Krea 2 prompt", message)
        self.assertNotIn("H3 prompt", message)
        self.assertNotIn("<Audio", message)
        self.assertIsNone(assembled["input"]["duration_seconds"])

    def test_an_image_refinement_needs_no_duration_or_aspect_ratio(self):
        payload = {
            "mode": "Anima",
            "session_id": SESSION,
            "current_prompt": "1girl, smile",
            "instruction": "colder light",
        }
        assembled = assemble_refinement(payload, None)
        self.assertEqual(assembled["input"]["variant"], "turbo")

    def test_a_refinement_carries_the_document_and_the_goals(self):
        ledger = [goals.new_goal("keep her skirt red", goals.KIND_PRESENCE, must_include=("red skirt",))]
        for mode in ("T2VA", "Reference", "Krea2", "Anima"):
            assembled = self.refine(mode, generic=doc(), goals=ledger)
            message = user_message(assembled)
            self.assertIn("Established facts", message, mode)
            self.assertIn("keep her skirt red", message, mode)
            self.assertEqual(assembled["input"]["generic"]["fields"], doc()["fields"], mode)
            self.assertEqual(len(assembled["input"]["goals"]), 1, mode)

    def test_a_refinement_runs_under_the_same_flags_as_the_generation(self):
        for mode in ("T2VA", "Krea2"):
            loose = self.refine(mode)
            self.assertTrue(loose["input"]["story"], mode)
            strict = self.refine(mode, nsfw=False, story=False)
            self.assertIn("do not invent", strict["system_prompt"]["content"].lower(), mode)


if __name__ == "__main__":
    unittest.main()
