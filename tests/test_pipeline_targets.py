"""End to end through the pipeline: goal verification, repair, output splitting.

Reuses the characterization backend stub, so these exercise the real
`run_h3_pipeline` -- the audit gate, the verifier call, the single repair pass and
the result shape -- with scripted model answers.
"""
from __future__ import annotations

import json
import unittest

from backend import generic, goals
from test_generation_characterization import (  # noqa: E402  (shared stub)
    _CharacterizedBackend,
    model_info,
    response,
    runtime_plan,
)

GOOD_PROSE = (
    "A ceramic vase of white ranunculus on a marble countertop, water beading on the stone, warm late "
    "afternoon light raking in from a window on the left, shallow depth of field, editorial product "
    "photography on medium format film, muted greens and warm neutrals, crisp detail on the petal edges."
)
GOOD_TAGS = (
    "Positive: masterpiece, best quality, safe, 1girl, smile, brown hair, santa costume, red gloves, "
    "looking at viewer, simple background\n"
    "Negative: worst quality, low quality, nsfw, explicit, blurry"
)


def request(mode, *, doc=None, goal_list=(), variant=None, brief="a portrait"):
    return {
        "messages": [
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": f"Creative brief:\n{brief}"},
        ],
        "media_inputs": [],
        "input": {
            "mode": mode,
            "duration_seconds": None,
            "creative_brief": brief,
            "generic": doc,
            "goals": list(goal_list),
            "variant": variant,
            "nsfw": True,
            "story": True,
        },
    }


def run(backend, assembled):
    return backend.generate(
        model_info(),
        assembled,
        "pipeline-session",
        thinking=False,
        seed=None,
        unload_after=False,
        runtime_plan=runtime_plan(),
    )


class ImageTargetPipelineTests(unittest.TestCase):
    def test_a_clean_krea_prompt_needs_no_repair(self):
        backend = _CharacterizedBackend([response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40)])
        result = run(backend, request("Krea2"))
        self.assertEqual(result["prompt"], GOOD_PROSE)
        self.assertFalse(result["format_repair_attempted"])
        self.assertEqual(result["negative_prompt"], "")
        self.assertTrue(result["prompt_audit"]["official_format_pass"])

    def test_a_malformed_krea_prompt_is_repaired_once(self):
        backend = _CharacterizedBackend([
            response("## Subject\n- a vase\n- a countertop", prompt_tokens=10, completion_tokens=8),
            response(GOOD_PROSE, prompt_tokens=12, completion_tokens=40),
        ])
        result = run(backend, request("Krea2"))
        self.assertTrue(result["format_repair_attempted"])
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["prompt"], GOOD_PROSE)
        self.assertEqual(result["format_repair_method"], "narrow text correction")

    def test_a_failed_repair_keeps_the_original_and_says_why(self):
        backend = _CharacterizedBackend([
            response("## Subject\nshort", prompt_tokens=10, completion_tokens=8),
            response("### still a heading", prompt_tokens=12, completion_tokens=8),
        ])
        result = run(backend, request("Krea2"))
        self.assertTrue(result["format_repair_attempted"])
        self.assertFalse(result["format_repair_applied"])
        self.assertEqual(result["prompt"], "## Subject\nshort")
        self.assertIn("still failed", result["format_repair_failure"])

    def test_anima_output_is_split_into_two_fields(self):
        backend = _CharacterizedBackend([response(GOOD_TAGS, prompt_tokens=10, completion_tokens=30)])
        result = run(backend, request("Anima", variant="base"))
        self.assertTrue(result["prompt"].startswith("masterpiece, best quality, safe, 1girl"))
        self.assertEqual(result["negative_prompt"], "worst quality, low quality, nsfw, explicit, blurry")
        self.assertNotIn("Positive:", result["prompt"])

    def test_the_aesthetic_variant_score_tag_rule_drives_a_repair(self):
        bad = "Positive: masterpiece, score_9, safe, 1girl, smile, brown hair\nNegative: score_1, worst quality"
        backend = _CharacterizedBackend([
            response(bad, prompt_tokens=10, completion_tokens=20),
            response(GOOD_TAGS, prompt_tokens=12, completion_tokens=30),
        ])
        result = run(backend, request("Anima", variant="aesthetic"))
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["prompt_audit"]["score_tags"], [])


class LockAndGoalPipelineTests(unittest.TestCase):
    def doc(self):
        return generic.set_field(
            generic.new_doc(), "wardrobe", "a red pleated skirt", generic.ORIGIN_ASSET
        )

    def test_a_dropped_locked_fact_triggers_a_repair_that_quotes_the_value(self):
        fixed = GOOD_PROSE + " She wears a red pleated skirt."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(fixed, prompt_tokens=12, completion_tokens=44),
        ])
        result = run(backend, request("Krea2", doc=self.doc()))
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["prompt"], fixed)
        self.assertIn("a red pleated skirt", result["format_repair_reason"])

    def test_a_deterministic_goal_is_checked_without_a_verifier_call(self):
        ledger = [goals.new_goal("follow the clothing colours in the image", goals.KIND_FIELD, fields=("wardrobe",))]
        prose = GOOD_PROSE + " She wears a red pleated skirt."
        backend = _CharacterizedBackend([response(prose, prompt_tokens=10, completion_tokens=44)])
        result = run(backend, request("Krea2", doc=self.doc(), goal_list=ledger))
        self.assertEqual(len(backend.chat_handler.calls), 1)
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)
        self.assertEqual(result["goal_verification_tokens"], 0)

    def test_an_unmet_deterministic_goal_is_repaired(self):
        ledger = [goals.new_goal("name the bakery sign", goals.KIND_PRESENCE, must_include=("Pane e Sale",))]
        fixed = GOOD_PROSE + ' A sign reads "Pane e Sale".'
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(fixed, prompt_tokens=12, completion_tokens=44),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)

    def test_a_judged_goal_costs_one_verifier_call_and_can_pass(self):
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        verdict = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": True, "reason": "editorial, not an advert"}]})
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(verdict, prompt_tokens=8, completion_tokens=12),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertEqual(len(backend.chat_handler.calls), 2)
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)
        self.assertEqual(result["goal_verification_tokens"], 12)
        self.assertFalse(result["format_repair_attempted"])

    def test_an_unmet_judged_goal_drives_the_repair_pass(self):
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        verdict = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        fixed = GOOD_PROSE + " Grain and imperfection throughout, no product gloss."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(verdict, prompt_tokens=8, completion_tokens=12),
            response(fixed, prompt_tokens=12, completion_tokens=44),
            # The re-audit of the correction re-runs the pending judged goal.
            response(json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": True, "reason": "fine"}]}),
                     prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertTrue(result["format_repair_attempted"])
        self.assertIn("reads like an advert", result["format_repair_reason"])

    def test_goal_verification_survives_a_backend_with_no_output_limit(self):
        """The external llama.cpp backend leaves max_tokens to the server.

        Live regression: a judged goal made every compile fail with an int(None)
        crash, because the verifier call assumed a numeric budget.
        """
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        verdict = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": True, "reason": "fine"}]})
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(verdict, prompt_tokens=8, completion_tokens=10),
        ])
        plan = {**runtime_plan(), "max_output_tokens": None}
        result = backend.generate(
            model_info(), request("Krea2", goal_list=ledger), "pipeline-session",
            thinking=False, seed=None, unload_after=False, runtime_plan=plan,
        )
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)

    def test_a_goal_driven_repair_is_confirmed_to_have_fixed_the_goal(self):
        """Re-auditing resets judged goals to pending; the repair must be re-checked.

        Otherwise a repair fired for an unmet goal is accepted because the goal
        reverted to pending, and the user is told it "could not be verified".
        """
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        unmet = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        met = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": True, "reason": "editorial now"}]})
        fixed = GOOD_PROSE + " Grain and imperfection throughout, no product gloss."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(unmet, prompt_tokens=8, completion_tokens=12),
            response(fixed, prompt_tokens=12, completion_tokens=44),
            response(met, prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["prompt"], fixed)
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)
        self.assertEqual(result["goals"][0]["reason"], "editorial now")

    def test_a_repair_that_does_not_fix_the_goal_is_rejected(self):
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        unmet = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        attempt = GOOD_PROSE + " Now with even more gloss."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(unmet, prompt_tokens=8, completion_tokens=12),
            response(attempt, prompt_tokens=12, completion_tokens=44),
            response(unmet, prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertFalse(result["format_repair_applied"])
        self.assertEqual(result["prompt"], GOOD_PROSE)
        self.assertIn("still failed", result["format_repair_failure"])
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_UNMET)

    def test_an_unverifiable_judged_goal_stays_pending_rather_than_passing(self):
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response("I think it's fine!", prompt_tokens=8, completion_tokens=6),
        ])
        result = run(backend, request("Krea2", goal_list=ledger))
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_PENDING)
        self.assertFalse(result["format_repair_attempted"])

    def test_the_h3_target_reports_the_same_goal_shape(self):
        ledger = [goals.new_goal("name the bakery sign", goals.KIND_PRESENCE, must_include=("Pane e Sale",))]
        prompt = (
            "subject_definitions:\nN/A\n\nsummary:\n[text to video] A shot.\n\n"
            "retention_analysis:\nN/A\n\ndetailed_description:\n[Shot 1] A sign reads \"Pane e Sale\".\n\n"
            "overall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
        )
        backend = _CharacterizedBackend([response(prompt, prompt_tokens=10, completion_tokens=40)])
        assembled = request("T2VA", goal_list=ledger)
        assembled["input"]["duration_seconds"] = 5
        result = run(backend, assembled)
        self.assertEqual(result["goals"][0]["verdict"], goals.VERDICT_MET)
        self.assertFalse(result["format_repair_attempted"])


if __name__ == "__main__":
    unittest.main()
