"""Krea 2 and Anima: output shape, the variant trap, and the safety tags.

The Anima variant cases are the ones worth having: per its model card, a score
tag is right on Base, harmful on Aesthetic and inert on Turbo, and nothing in
the output looks wrong when you get it wrong.
"""
from __future__ import annotations

import unittest

from backend import generic
from backend.targets import anima, krea2


def assembled(mode, brief="a portrait", *, doc=None, goals=(), variant=None, nsfw=True, story=True):
    return {
        "messages": [{"role": "user", "content": f"Creative brief:\n{brief}"}],
        "media_inputs": [],
        "input": {
            "mode": mode,
            "creative_brief": brief,
            "generic": doc,
            "goals": list(goals),
            "variant": variant,
            "nsfw": nsfw,
            "story": story,
        },
    }


GOOD_PROSE = (
    "A ceramic vase of white ranunculus on a marble countertop, water beading on the stone, warm late "
    "afternoon light raking in from a window on the left, shallow depth of field with the kitchen behind "
    "falling into soft bokeh, editorial product photography on medium format film, muted greens and warm "
    "neutrals, high detail on the petal edges and the cool grain of the marble."
)
GOOD_TAGS = (
    "Positive: masterpiece, best quality, safe, 1girl, oomuro sakurako, yuru yuri, @nnn yryr, smile, "
    "brown hair, santa costume, red gloves, looking at viewer, simple background, white background\n"
    "Negative: worst quality, low quality, nsfw, explicit, blurry, jpeg artifacts"
)


class Krea2AuditTests(unittest.TestCase):
    def test_a_single_prose_paragraph_passes(self):
        result = krea2.audit(GOOD_PROSE, assembled("Krea2"))
        self.assertTrue(result["official_format_pass"])
        self.assertFalse(result["repair_required"])
        self.assertEqual(result["paragraphs"], 1)

    def test_multiple_paragraphs_are_repairable(self):
        result = krea2.audit(GOOD_PROSE + "\n\nAlso a second paragraph about the light.", assembled("Krea2"))
        self.assertTrue(result["repair_required"])
        self.assertIn("one paragraph", " ".join(result["format_failures"]))

    def test_headings_and_bullets_are_repairable(self):
        for draft in (
            "## Subject\n" + GOOD_PROSE,
            "- a vase\n- a countertop",
            "Subject: a vase on a countertop, warm light, editorial photography, shallow depth of field",
        ):
            result = krea2.audit(draft, assembled("Krea2"))
            self.assertTrue(result["repair_required"], draft[:40])

    def test_a_fenced_answer_alone_is_not_a_failure(self):
        """Packaging is stripped, not repaired -- a repair turn for a code fence is waste."""
        result = krea2.audit(f"```\n{GOOD_PROSE}\n```", assembled("Krea2"))
        self.assertEqual(result["format_failures"], [])
        self.assertFalse(result["repair_required"])

    def test_meta_preamble_is_repairable(self):
        result = krea2.audit("This image shows " + GOOD_PROSE, assembled("Krea2"))
        self.assertTrue(result["meta_preamble"])
        self.assertTrue(result["repair_required"])

    def test_a_too_short_prompt_is_repairable(self):
        result = krea2.audit("a vase", assembled("Krea2"))
        self.assertTrue(result["repair_required"])
        self.assertIn("too short", " ".join(result["format_failures"]))

    def test_visible_text_must_be_quoted(self):
        doc = generic.set_field(generic.new_doc(), "visible_text", "Pane e Sale", generic.ORIGIN_USER)
        unquoted = krea2.audit(GOOD_PROSE + " The sign above reads Pane e Sale.", assembled("Krea2", doc=doc))
        self.assertTrue(unquoted["unquoted_visible_text"])
        quoted = krea2.audit(GOOD_PROSE + ' The sign above reads "Pane e Sale".', assembled("Krea2", doc=doc))
        self.assertFalse(quoted["unquoted_visible_text"])

    def test_a_dropped_locked_fact_is_repairable(self):
        doc = generic.set_field(generic.new_doc(), "wardrobe", "a red pleated skirt", generic.ORIGIN_ASSET)
        result = krea2.audit(GOOD_PROSE, assembled("Krea2", doc=doc))
        self.assertEqual(result["lock_violations"], ["wardrobe"])
        self.assertTrue(result["repair_required"])
        self.assertIn("a red pleated skirt", " ".join(result["shared_failures"]))

    def test_a_very_long_prompt_warns_without_forcing_a_repair(self):
        result = krea2.audit(GOOD_PROSE + (" more descriptive words" * 200), assembled("Krea2"))
        self.assertTrue(result["quality_warnings"])
        self.assertFalse(result["repair_required"])


class AnimaParseTests(unittest.TestCase):
    def test_the_two_blocks_are_split(self):
        parsed = anima.parse_output(GOOD_TAGS)
        self.assertTrue(parsed["prompt"].startswith("masterpiece, best quality, safe, 1girl"))
        self.assertEqual(parsed["negative_prompt"], "worst quality, low quality, nsfw, explicit, blurry, jpeg artifacts")

    def test_bold_and_alternate_labels_are_accepted(self):
        parsed = anima.parse_output("**Positive prompt:** 1girl, smile\n**Negative:** low quality")
        self.assertEqual(parsed["prompt"], "1girl, smile")
        self.assertEqual(parsed["negative_prompt"], "low quality")

    def test_an_unlabelled_answer_yields_an_empty_negative(self):
        parsed = anima.parse_output("1girl, smile, brown hair")
        self.assertEqual(parsed["negative_prompt"], "")


class AnimaAuditTests(unittest.TestCase):
    def test_a_well_formed_pair_passes(self):
        result = anima.audit(GOOD_TAGS, assembled("Anima", variant="base", nsfw=False))
        self.assertEqual(result["format_failures"], [])
        self.assertFalse(result["repair_required"])

    def test_a_missing_negative_block_is_repairable(self):
        result = anima.audit("Positive: 1girl, smile, brown hair, safe, masterpiece, best quality", assembled("Anima"))
        self.assertTrue(result["repair_required"])
        self.assertIn("Negative: line is missing", " ".join(result["format_failures"]))

    def test_underscores_outside_score_tags_are_repairable(self):
        draft = "Positive: masterpiece, best quality, safe, 1girl, brown_hair, looking_at_viewer, smile\nNegative: worst quality"
        result = anima.audit(draft, assembled("Anima"))
        self.assertTrue(result["repair_required"])
        joined = " ".join(result["format_failures"])
        self.assertIn("brown_hair", joined)
        self.assertIn("looking_at_viewer", joined)

    def test_score_tags_are_not_flagged_as_underscore_misuse(self):
        draft = "Positive: masterpiece, score_7, safe, 1girl, smile, brown hair\nNegative: score_1, worst quality"
        result = anima.audit(draft, assembled("Anima", variant="base"))
        self.assertEqual(result["format_failures"], [])

    def test_uppercase_tags_are_repairable_but_prose_is_not(self):
        bad = "Positive: masterpiece, best quality, safe, 1girl, Brown Hair, smile, red gloves\nNegative: worst quality"
        self.assertTrue(anima.audit(bad, assembled("Anima"))["repair_required"])
        prose = (
            "Positive: masterpiece, best quality, safe, 1girl, smile, brown hair, "
            "An anime girl stands in a snowy courtyard at dusk, lit from behind by a paper lantern.\n"
            "Negative: worst quality, low quality"
        )
        self.assertEqual(anima.audit(prose, assembled("Anima"))["format_failures"], [])

    def test_an_artist_tag_must_start_with_the_at_sign(self):
        draft = "Positive: masterpiece, best quality, safe, 1girl, art by @someone, smile, brown hair\nNegative: worst quality"
        result = anima.audit(draft, assembled("Anima"))
        self.assertIn("must start with @", " ".join(result["format_failures"]))

    def test_aesthetic_variant_rejects_score_tags_in_either_prompt(self):
        draft = "Positive: masterpiece, score_9, safe, 1girl, smile, brown hair\nNegative: score_1, worst quality"
        result = anima.audit(draft, assembled("Anima", variant="aesthetic"))
        self.assertTrue(result["repair_required"])
        self.assertIn("must not use score tags", " ".join(result["format_failures"]))

    def test_base_variant_only_warns_when_score_tags_are_absent(self):
        result = anima.audit(GOOD_TAGS, assembled("Anima", variant="base", nsfw=False))
        self.assertTrue(any("score tag" in warning for warning in result["quality_warnings"]))
        self.assertFalse(result["repair_required"])

    def test_turbo_variant_warns_that_the_negative_is_inert(self):
        result = anima.audit(GOOD_TAGS, assembled("Anima", variant="turbo"))
        self.assertTrue(any("CFG 1" in warning for warning in result["quality_warnings"]))
        self.assertFalse(result["repair_required"])

    def test_naughty_off_forces_the_safe_tag(self):
        draft = "Positive: masterpiece, best quality, 1girl, smile, brown hair, red gloves\nNegative: worst quality, nsfw, explicit"
        result = anima.audit(draft, assembled("Anima", variant="base", nsfw=False))
        self.assertIn("must include the safe tag", " ".join(result["format_failures"]))

    def test_naughty_off_requires_the_negative_safety_tags(self):
        draft = "Positive: masterpiece, best quality, safe, 1girl, smile, brown hair\nNegative: worst quality"
        result = anima.audit(draft, assembled("Anima", variant="base", nsfw=False))
        self.assertIn("negative prompt must include nsfw, explicit", " ".join(result["format_failures"]))

    def test_on_turbo_the_missing_negative_safety_tags_are_only_a_warning(self):
        """CFG 1 ignores the negative, so spending a repair turn on it is waste."""
        draft = "Positive: masterpiece, best quality, safe, 1girl, smile, brown hair\nNegative: worst quality"
        result = anima.audit(draft, assembled("Anima", variant="turbo", nsfw=False))
        self.assertEqual(result["format_failures"], [])
        self.assertTrue(any("ignores the negative" in warning for warning in result["quality_warnings"]))

    def test_an_explicit_brief_tagged_safe_is_a_contradiction(self):
        draft = "Positive: masterpiece, best quality, safe, 1girl, smile, brown hair\nNegative: worst quality"
        result = anima.audit(draft, assembled("Anima", brief="an explicit nude portrait", variant="base"))
        self.assertIn("tagged safe", " ".join(result["format_failures"]))

    def test_locks_are_checked_across_both_prompts(self):
        doc = generic.set_field(generic.new_doc(), "wardrobe", "santa costume", generic.ORIGIN_ASSET)
        self.assertEqual(anima.audit(GOOD_TAGS, assembled("Anima", doc=doc))["lock_violations"], [])
        other = generic.set_field(generic.new_doc(), "wardrobe", "a leather flight jacket", generic.ORIGIN_ASSET)
        self.assertEqual(anima.audit(GOOD_TAGS, assembled("Anima", doc=other))["lock_violations"], ["wardrobe"])


class RepairTests(unittest.TestCase):
    def test_krea_repair_names_the_violations_and_keeps_the_request(self):
        result = krea2.audit("## heading\nshort", assembled("Krea2"))
        plan = krea2.repair_plan(assembled("Krea2"), [], "## heading\nshort", result)
        self.assertIn("narrow correction pass", plan["messages"][0]["content"])
        self.assertIn("Creative brief:", plan["messages"][1]["content"])
        self.assertIn("DRAFT TO CORRECT", plan["messages"][1]["content"])

    def test_krea_repair_is_refused_when_the_correction_still_fails(self):
        request = assembled("Krea2")
        accepted, _audit, failure = krea2.accept_repair(request, "draft", "## still a heading", {})
        self.assertFalse(accepted)
        self.assertIn("still failed", failure)

    def test_krea_repair_is_accepted_when_clean(self):
        request = assembled("Krea2")
        accepted, _audit, failure = krea2.accept_repair(request, "bad draft", GOOD_PROSE, {})
        self.assertTrue(accepted)
        self.assertIsNone(failure)

    def test_anima_repair_restates_the_variant_rule(self):
        request = assembled("Anima", variant="aesthetic")
        draft = "Positive: score_9, 1girl\nNegative: score_1"
        plan = anima.repair_plan(request, [], draft, anima.audit(draft, request))
        self.assertIn("Anima-Aesthetic", plan["messages"][0]["content"])
        self.assertIn("Never use a score_* tag", plan["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
