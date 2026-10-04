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


def request(mode, *, doc=None, goal_list=(), variant=None, brief="a portrait", repair_attempts=1):
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
            # One pass unless a test is about the loop itself, so a scripted
            # answer list means what it says.
            "repair_attempts": repair_attempts,
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

    def test_an_image_target_is_not_audited_for_sound(self):
        """Measured: a Krea 2 compile was repaired for "Soundscape must be: Silent".

        A still cannot carry a sound fact, so the repair could never fix it --
        every attempt was spent and the user was told the prompt had dropped
        something it could never have held.
        """
        doc = generic.set_field(generic.new_doc(), "soundscape", "Silent", generic.ORIGIN_USER)
        doc = generic.set_field(doc, "subject", "a woman on a bridge", generic.ORIGIN_USER)
        backend = _CharacterizedBackend([response(GOOD_PROSE + " A woman on a bridge.", prompt_tokens=10, completion_tokens=40)])
        result = run(backend, request("Krea2", doc=doc))
        self.assertFalse(result["format_repair_attempted"], "nothing to repair: the fact was never askable")
        self.assertNotIn("soundscape", result["prompt_audit"].get("lock_violations", []))

    def test_a_video_target_is_still_audited_for_sound(self):
        """Scoped to what the shape can hold -- H3 carries audio, so it answers for it."""
        from backend.targets import base
        for field in ("soundscape", "dialogue", "music"):
            self.assertTrue(base._auditable(field, {"input": {"mode": "T2VA"}}), field)
            self.assertFalse(base._auditable(field, {"input": {"mode": "Krea2"}}), field)
        # Everything else is audited on every target, as before.
        for field in ("subject", "wardrobe", "pose", "staging", "wardrobe#2"):
            self.assertTrue(base._auditable(field, {"input": {"mode": "Krea2"}}), field)

    def test_the_repair_loop_keeps_trying_until_the_audit_passes(self):
        """The user sets how many corrections to spend before being asked."""
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        unmet = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        met = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": True, "reason": "plain enough"}]})
        second_try = GOOD_PROSE + " Quieter now."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(unmet, prompt_tokens=8, completion_tokens=12),
            response(GOOD_PROSE + " Glossier.", prompt_tokens=12, completion_tokens=44),   # repair 1
            response(unmet, prompt_tokens=8, completion_tokens=10),
            response(second_try, prompt_tokens=12, completion_tokens=44),                  # repair 2
            response(met, prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=ledger, repair_attempts=3))
        self.assertTrue(result["format_repair_applied"])
        self.assertEqual(result["format_repair_attempts"], 2, "it stopped as soon as the audit passed")
        self.assertEqual(result["prompt"], second_try)
        self.assertIsNone(result["format_repair_failure"])

    def test_the_loop_stops_at_the_user_s_budget_and_hands_back_the_original(self):
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        unmet = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(unmet, prompt_tokens=8, completion_tokens=12),
            response(GOOD_PROSE + " One.", prompt_tokens=12, completion_tokens=44),
            response(unmet, prompt_tokens=8, completion_tokens=10),
            response(GOOD_PROSE + " Two.", prompt_tokens=12, completion_tokens=44),
            response(unmet, prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=ledger, repair_attempts=2))
        self.assertEqual(result["format_repair_attempts"], 2)
        self.assertEqual(result["format_repair_allowed"], 2)
        self.assertFalse(result["format_repair_applied"])
        self.assertEqual(result["prompt"], GOOD_PROSE, "the model's own writing, not a spliced draft that failed")
        self.assertIn("still failed", result["format_repair_failure"])
        self.assertEqual(result["format_repair_best_attempt"], 0, "nothing improved, so a tie goes to the first draft")

    def test_an_exhausted_budget_keeps_the_best_draft_not_the_first(self):
        """Measured: the loop used to discard every pass that improved the prompt.

        With two unmet goals and one correction that fixes one of them, the old
        code reverted to the first draft AND to its audit, so the user was shown
        two unmet goals for a draft that had one. Spending more corrections
        could never make the counts come down.
        """
        a = goals.new_goal("do not make it feel like a commercial")
        b = goals.new_goal("keep the vase ceramic")
        both_unmet = json.dumps({"verdicts": [
            {"id": a["id"], "met": False, "reason": "reads like an advert"},
            {"id": b["id"], "met": False, "reason": "vase material dropped"},
        ]})
        one_fixed = json.dumps({"verdicts": [
            {"id": a["id"], "met": True, "reason": "plain enough"},
            {"id": b["id"], "met": False, "reason": "vase material still dropped"},
        ]})
        better = GOOD_PROSE + " Quieter now."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(both_unmet, prompt_tokens=8, completion_tokens=12),
            response(better, prompt_tokens=12, completion_tokens=44),
            response(one_fixed, prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=[a, b], repair_attempts=1))
        self.assertFalse(result["format_repair_applied"], "one goal is still unmet")
        self.assertEqual(result["prompt"], better, "the improved draft, not the first one")
        self.assertEqual(result["format_repair_best_attempt"], 1)
        unmet = [
            goal for goal in result["prompt_audit"]["goals"]
            if goal.get("enabled") and goal.get("verdict") == "unmet"
        ]
        self.assertEqual(len(unmet), 1, "the counts shown must describe the draft actually handed back")

    def test_a_correction_that_makes_it_worse_does_not_become_the_answer(self):
        """The reported symptom: the counts went UP as corrections were spent.

        Correction 1 fixes a goal, correction 2 loses it again. The loop keeps
        the improved middle draft rather than the last one it happened to write,
        and the failure text describes the draft handed back.
        """
        a = goals.new_goal("do not make it feel like a commercial")
        b = goals.new_goal("keep the vase ceramic")

        def verdicts(a_met, b_met):
            return json.dumps({"verdicts": [
                {"id": a["id"], "met": a_met, "reason": "advert" if not a_met else "plain"},
                {"id": b["id"], "met": b_met, "reason": "material" if not b_met else "ceramic"},
            ]})

        improved = GOOD_PROSE + " Quieter now."
        regressed = GOOD_PROSE + " Glossier again."
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(verdicts(False, False), prompt_tokens=8, completion_tokens=12),
            response(improved, prompt_tokens=12, completion_tokens=44),
            response(verdicts(True, False), prompt_tokens=8, completion_tokens=10),
            response(regressed, prompt_tokens=12, completion_tokens=44),
            response(verdicts(False, False), prompt_tokens=8, completion_tokens=10),
        ])
        result = run(backend, request("Krea2", goal_list=[a, b], repair_attempts=2))
        self.assertFalse(result["format_repair_applied"])
        self.assertEqual(result["format_repair_attempts"], 2)
        self.assertEqual(result["prompt"], improved, "not the last draft, and not the first")
        self.assertEqual(result["format_repair_best_attempt"], 1)
        unmet = [
            goal for goal in result["prompt_audit"]["goals"]
            if goal.get("enabled") and goal.get("verdict") == "unmet"
        ]
        self.assertEqual(len(unmet), 1, "the counts come down and stay down")
        self.assertIn("material", result["format_repair_failure"], "describes the draft on screen")

    def test_zero_attempts_never_calls_the_model_again(self):
        """Some users would rather see the first draft and fix it themselves."""
        ledger = [goals.new_goal("do not make it feel like a commercial")]
        unmet = json.dumps({"verdicts": [{"id": ledger[0]["id"], "met": False, "reason": "reads like an advert"}]})
        backend = _CharacterizedBackend([
            response(GOOD_PROSE, prompt_tokens=10, completion_tokens=40),
            response(unmet, prompt_tokens=8, completion_tokens=12),
        ])
        result = run(backend, request("Krea2", goal_list=ledger, repair_attempts=0))
        self.assertFalse(result["format_repair_attempted"])
        self.assertEqual(result["format_repair_attempts"], 0)
        self.assertEqual(result["prompt"], GOOD_PROSE)

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
