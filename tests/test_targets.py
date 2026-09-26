"""Registry invariants and the flag matrix.

The byte-identical assertions matter most: they are what proves the two flags
edit the existing contracts rather than replacing them, and that turning both
off still produces exactly the prompts this writer shipped with.
"""
from __future__ import annotations

import unittest

from backend import targets
from backend.guides import GUIDES, load_guide
from backend.system_prompts import (
    MUSIC3_LYRICS_SYSTEM_WRAPPER,
    MUSIC3_SYSTEM_WRAPPER,
    REFERENCE_SYSTEM_WRAPPER,
    SYSTEM_WRAPPER,
    system_prompt_for_mode,
)
from backend.targets.base import TargetError, compose


class RegistryTests(unittest.TestCase):
    def test_every_mode_has_exactly_one_owner(self):
        owners = {}
        for target in targets.TARGETS:
            for mode in target.modes:
                self.assertNotIn(mode.id, owners, f"{mode.id} is claimed twice")
                owners[mode.id] = target.id
        self.assertEqual(set(owners), set(targets.all_modes()))

    def test_existing_modes_are_unchanged(self):
        """Mode ids are a stored contract: drafts, overrides and tests key off them."""
        for mode in ("T2VA", "I2VA", "FL2VA", "L2VA", "Reference", "Music3", "Music3Lyrics"):
            self.assertIn(mode, targets.all_modes())
        self.assertNotIn("Music3Lyrics", targets.generation_modes())

    def test_new_image_targets_are_registered(self):
        self.assertEqual(targets.target_for_mode("Krea2").id, "krea2")
        self.assertEqual(targets.target_for_mode("Anima").id, "anima")
        for mode in ("Krea2", "Anima"):
            self.assertEqual(targets.target_for_mode(mode).workspace, "image")
            self.assertEqual(targets.mode_spec(mode).reference_limits, {"image": 4})

    def test_every_declared_guide_resolves_and_verifies(self):
        for mode in targets.all_modes():
            for guide_id in targets.guide_ids_for_mode(mode):
                self.assertIn(guide_id, GUIDES)
                self.assertTrue(load_guide(guide_id)["content"].strip())

    def test_unknown_mode_raises_rather_than_defaulting(self):
        with self.assertRaises(TargetError):
            targets.target_for_mode("Krea3")

    def test_mode_limits_cover_every_generation_mode(self):
        self.assertEqual(set(targets.mode_limits()), set(targets.generation_modes()))

    def test_image_targets_do_not_declare_a_duration(self):
        for mode in ("Krea2", "Anima"):
            self.assertFalse(targets.target_for_mode(mode).declares("duration_seconds"))
        self.assertTrue(targets.target_for_mode("T2VA").declares("duration_seconds"))

    def test_only_anima_has_variants(self):
        self.assertEqual(targets.target_by_id("anima").variants, ("base", "aesthetic", "turbo"))
        self.assertEqual(targets.target_by_id("anima").default_variant, "turbo")
        for target_id in ("h3", "krea2", "music3"):
            self.assertEqual(targets.target_by_id(target_id).variants, ())

    def test_catalog_is_json_shaped_for_the_studio(self):
        catalog = targets.catalog()
        self.assertEqual({item["id"] for item in catalog}, {"h3", "krea2", "anima", "music3"})
        krea = next(item for item in catalog if item["id"] == "krea2")
        self.assertEqual(krea["workspace"], "image")
        self.assertEqual(krea["output_shape"], "prose")
        guide_titles = [guide["title"] for guide in krea["modes"][0]["guides"]]
        self.assertIn("Krea 2 Prompting Guidelines", guide_titles)


class FlagCompositionTests(unittest.TestCase):
    def test_flags_off_reproduces_the_shipped_wrappers_byte_for_byte(self):
        for mode, wrapper in (
            ("T2VA", SYSTEM_WRAPPER),
            ("I2VA", SYSTEM_WRAPPER),
            ("Reference", REFERENCE_SYSTEM_WRAPPER),
            ("Music3", MUSIC3_SYSTEM_WRAPPER),
            ("Music3Lyrics", MUSIC3_LYRICS_SYSTEM_WRAPPER),
        ):
            self.assertEqual(system_prompt_for_mode(mode), wrapper.strip(), mode)

    def test_story_builder_removes_the_no_invention_rule_and_licenses_invention(self):
        strict = system_prompt_for_mode("T2VA")
        loose = system_prompt_for_mode("T2VA", story=True)
        self.assertIn("do not invent unsupported subject actions", strict)
        self.assertNotIn("do not invent unsupported subject actions", loose)
        self.assertIn("Where the brief leaves the scene unspecified, design it", loose)

    def test_story_builder_on_reference_keeps_the_source_motion_order(self):
        loose = system_prompt_for_mode("Reference", story=True)
        self.assertIn("Preserve the source motion order.", loose)
        self.assertNotIn("but do not invent unsupported subject actions", loose)

    def test_naughty_adds_permission_without_removing_anything(self):
        base = system_prompt_for_mode("T2VA")
        naughty = system_prompt_for_mode("T2VA", nsfw=True)
        self.assertTrue(naughty.startswith(base))
        self.assertIn("Adult, explicit or otherwise mature content is permitted", naughty)

    def test_krea_naughty_swaps_the_upstream_dignity_clause(self):
        polite = system_prompt_for_mode("Krea2")
        naughty = system_prompt_for_mode("Krea2", nsfw=True)
        self.assertIn("assume clothing covers genitals", polite)
        self.assertNotIn("assume clothing covers genitals", naughty)
        self.assertIn("Adult or explicit content is permitted", naughty)

    def test_anima_variant_drives_the_score_tag_rule(self):
        aesthetic = system_prompt_for_mode("Anima", variant="aesthetic")
        base = system_prompt_for_mode("Anima", variant="base")
        turbo = system_prompt_for_mode("Anima", variant="turbo")
        self.assertIn("Never use a score_* tag", aesthetic)
        self.assertIn("Include a human-score quality tag and a score_* tag", base)
        self.assertIn("runs at CFG 1", turbo)

    def test_anima_safety_clause_follows_the_naughty_flag(self):
        self.assertIn("Include the safe tag", system_prompt_for_mode("Anima"))
        self.assertIn("do not add safe to a brief that asks for adult content",
                      system_prompt_for_mode("Anima", nsfw=True))

    def test_every_flag_combination_produces_a_usable_contract(self):
        for mode in targets.all_modes():
            variants = targets.target_for_mode(mode).variants or (None,)
            for nsfw in (False, True):
                for story in (False, True):
                    for variant in variants:
                        prompt = system_prompt_for_mode(mode, nsfw=nsfw, story=story, variant=variant)
                        self.assertGreater(len(prompt), 200, (mode, nsfw, story, variant))
                        self.assertNotIn("  ", prompt, (mode, nsfw, story, variant))

    def test_compose_refuses_to_silently_skip_a_missing_clause(self):
        """A reworded wrapper must break loudly, not disable the flag in silence."""
        with self.assertRaises(TargetError):
            compose("a contract without the clause", remove=("a clause that is not there",))


if __name__ == "__main__":
    unittest.main()
