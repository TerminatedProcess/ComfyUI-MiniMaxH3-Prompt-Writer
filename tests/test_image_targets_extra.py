"""SD tags and the vendor-prose targets (Qwen-Image, FLUX 3, MOSS-Image).

The cases worth having are the silent ones: a Pony prompt without its score
string renders fine and looks wrong, and a prose model handed its vendor's JSON
output format back as the "caption" renders the braces.
"""
from __future__ import annotations

import unittest

from backend import guides
from backend.assembly import assemble_request
from backend.targets import FLUX3, MOSS_IMAGE, QWEN_IMAGE, sdtags

SESSION = "11111111-2222-4333-8444-555555555555"


def _assembled(variant):
    return {
        "input": {"mode": "SDTags", "variant": variant, "nsfw": True},
        "messages": [{"role": "user", "content": "brief"}],
    }


class SDTagAuditTests(unittest.TestCase):
    def test_pony_needs_its_full_score_string(self):
        bad = "Positive: 1girl, solo, smile, red hair, beach, sunset\nNegative: lowres"
        result = sdtags.audit(bad, _assembled("pony"))
        self.assertTrue(result["repair_required"])
        good = f"Positive: {sdtags.PONY_SCORE_PREFIX}, source_anime, rating_safe, 1girl, smile, beach\nNegative: lowres"
        self.assertFalse(sdtags.audit(good, _assembled("pony"))["repair_required"])

    def test_score_tags_are_refused_off_pony(self):
        draft = "Positive: score_9, masterpiece, 1girl, solo, smile, beach\nNegative: worst quality"
        result = sdtags.audit(draft, _assembled("illustrious"))
        self.assertTrue(result["repair_required"])
        self.assertIn("score_9", " ".join(result["format_failures"]))

    def test_a_clean_illustrious_pair_passes(self):
        draft = "Positive: masterpiece, best quality, 1girl, solo, upper body, smile, (red hair:1.2)\nNegative: worst quality, lowres"
        self.assertFalse(sdtags.audit(draft, _assembled("illustrious"))["repair_required"])

    def test_nested_weights_fail(self):
        draft = "Positive: masterpiece, ((1girl)), solo, smile, beach, sunset\nNegative: lowres"
        self.assertTrue(sdtags.audit(draft, _assembled("sdxl"))["repair_required"])




class SDTagGuideTests(unittest.TestCase):
    def names(self, variant):
        assembled = assemble_request({"mode": "SDTags", "session_id": SESSION, "creative_brief": "1girl", "variant": variant})
        return [m.get("name") for m in assembled["messages"] if m["role"] == "system"]

    def test_each_variant_gets_only_its_own_guide(self):
        # Pony's description and the Illustrious card contradict each other on quality tags.
        self.assertIn("official_pony_prompting_rules", self.names("pony"))
        self.assertNotIn("official_illustrious_prompting_rules", self.names("pony"))
        self.assertIn("official_illustrious_prompting_rules", self.names("illustrious"))
        self.assertNotIn("official_pony_prompting_rules", self.names("illustrious"))
        self.assertEqual([n for n in self.names("sdxl") if n and n.startswith("official_")], [])


class ProseFamilyTests(unittest.TestCase):
    CAPTION = " ".join(["A weathered fisherman mends a green net on a wooden pier at dawn."] * 14)

    def _assembled(self, mode):
        return {"input": {"mode": mode, "nsfw": True}, "messages": [{"role": "user", "content": "brief"}]}

    def test_each_vendor_guide_excerpts_to_its_prompting_rules(self):
        self.assertIn("Image Prompt Rewriting Expert", guides.qwen_image_prompting_excerpt())
        self.assertNotIn("def polish_prompt_zh", guides.qwen_image_prompting_excerpt())
        self.assertIn("prompt upsampler", guides.flux3_prompting_excerpt())
        self.assertIn("Prompt Guidelines", guides.moss_image_prompting_excerpt())
        self.assertNotIn("Layout Prompts", guides.moss_image_prompting_excerpt())
        self.assertIn("cohesive", guides.moss_image_caption_excerpt())

    def test_a_good_caption_passes_and_json_echo_of_the_guide_format_fails(self):
        for target in (QWEN_IMAGE, FLUX3, MOSS_IMAGE):
            mode = target.modes[0].id
            self.assertFalse(target.audit(self.CAPTION, self._assembled(mode))["repair_required"], target.id)
            echoed = '{"rewritten_prompt": "' + self.CAPTION + '", "wh_ratio": "16:9"}'
            self.assertTrue(target.audit(echoed, self._assembled(mode))["repair_required"], target.id)
            self.assertTrue(target.audit("A fisherman.", self._assembled(mode))["repair_required"], target.id)

    def test_only_moss_returns_a_negative_and_it_never_carries_nsfw(self):
        self.assertEqual(QWEN_IMAGE.parse_output("x")["negative_prompt"], "")
        self.assertEqual(FLUX3.parse_output("x")["negative_prompt"], "")
        negative = MOSS_IMAGE.parse_output("x")["negative_prompt"]
        self.assertIn("worst quality", negative)
        self.assertNotIn("nsfw", negative)

    def test_assembly_sends_the_vendor_guide(self):
        session = SESSION
        expected = {
            "QwenImage": "official_qwen_image_prompt_enhancement",
            "Flux3": "official_flux3_prompt_upsampler",
            "MossImage": "official_moss_image_prompt_guidelines",
        }
        for mode, name in expected.items():
            assembled = assemble_request({"mode": mode, "session_id": session, "creative_brief": "a pier at dawn"})
            names = [m.get("name") for m in assembled["messages"] if m["role"] == "system"]
            self.assertIn(name, names, mode)


if __name__ == "__main__":
    unittest.main()
