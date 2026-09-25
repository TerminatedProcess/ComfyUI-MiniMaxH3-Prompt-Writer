"""The goal ledger: standing instructions that keep being checked.

The point of every test here is durability. A goal that is applied once and
forgotten is the bug; a goal that survives a regeneration, survives a target
switch, and reports honestly when it cannot be met is the feature.
"""
from __future__ import annotations

import unittest

from backend import generic, goals


def doc_with(**fields):
    doc = generic.new_doc()
    for key, (value, origin) in fields.items():
        doc = generic.set_field(doc, key, value, origin)
    return doc


class GoalShapeTests(unittest.TestCase):
    def test_a_field_goal_must_name_fields(self):
        with self.assertRaises(goals.GoalError):
            goals.new_goal("follow the clothing colours", goals.KIND_FIELD)

    def test_a_presence_goal_must_name_what_has_to_appear(self):
        with self.assertRaises(goals.GoalError):
            goals.new_goal("mention the bakery", goals.KIND_PRESENCE)

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(goals.GoalError):
            goals.new_goal("x", goals.KIND_FIELD, fields=("vibes",))

    def test_duplicate_wording_is_not_added_twice(self):
        first = goals.new_goal("keep it one continuous shot")
        ledger, added = goals.merge([first], [goals.new_goal("Keep it one continuous shot ")])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(added, [])

    def test_validate_round_trips_a_stored_ledger(self):
        ledger = [goals.new_goal("no brand names")]
        ledger[0]["verdict"] = goals.VERDICT_UNMET
        restored = goals.validate(ledger)
        self.assertEqual(restored[0]["verdict"], goals.VERDICT_UNMET)
        self.assertEqual(restored[0]["id"], ledger[0]["id"])

    def test_a_paused_goal_stops_being_checked(self):
        ledger = [goals.new_goal("mention the sign", goals.KIND_PRESENCE, must_include=("bakery",))]
        ledger = goals.toggle(ledger, ledger[0]["id"], False)
        _evaluated, violations, pending = goals.evaluate(ledger, None, "a prompt with no sign")
        self.assertEqual((violations, pending), ([], []))


class FieldGoalTests(unittest.TestCase):
    GOAL = ("make sure the prompt follows the clothing colours in the image", goals.KIND_FIELD)

    def ledger(self):
        return [goals.new_goal(*self.GOAL, fields=("wardrobe",))]

    def test_unmet_while_the_field_is_still_unspecified(self):
        """The exact gap that let the skirt come out blue."""
        evaluated, violations, _pending = goals.evaluate(self.ledger(), generic.new_doc(), "a prompt")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_UNMET)
        self.assertIn("Wardrobe is still unspecified", evaluated[0]["reason"])
        self.assertEqual(len(violations), 1)

    def test_unmet_when_the_field_was_invented_rather_than_observed(self):
        doc = doc_with(wardrobe=("a blue skirt", generic.ORIGIN_INVENTED))
        evaluated, violations, _pending = goals.evaluate(self.ledger(), doc, "a prompt")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_UNMET)
        self.assertIn("invented", evaluated[0]["reason"])
        self.assertTrue(violations)

    def test_met_once_the_field_comes_from_the_reference(self):
        doc = doc_with(wardrobe=("a red pleated skirt", generic.ORIGIN_ASSET))
        evaluated, violations, _pending = goals.evaluate(self.ledger(), doc, "a prompt")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_MET)
        self.assertIn("from the reference", evaluated[0]["reason"])
        self.assertEqual(violations, [])

    def test_met_by_a_deliberate_override_rather_than_fighting_it(self):
        """Tier 2 beats tier 1: the user's own choice satisfies the goal."""
        doc = generic.set_field(
            generic.new_doc(), "wardrobe", "a red skirt", generic.ORIGIN_OVERRIDE, observed="a blue skirt"
        )
        evaluated, violations, _pending = goals.evaluate(self.ledger(), doc, "a prompt")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_MET)
        self.assertIn("your override", evaluated[0]["reason"])
        self.assertEqual(violations, [])


class PresenceGoalTests(unittest.TestCase):
    def ledger(self):
        return [goals.new_goal("name the bakery sign", goals.KIND_PRESENCE, must_include=("Pane e Sale",))]

    def test_unmet_when_the_phrase_is_missing(self):
        evaluated, violations, _pending = goals.evaluate(self.ledger(), None, "a shopfront at dusk")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_UNMET)
        self.assertTrue(violations)

    def test_met_case_insensitively(self):
        evaluated, violations, _pending = goals.evaluate(
            self.ledger(), None, 'the sign reads "pane e sale" in gold leaf'
        )
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_MET)
        self.assertEqual(violations, [])


class JudgedGoalTests(unittest.TestCase):
    def setUp(self):
        self.ledger = [goals.new_goal("do not make it feel like a commercial")]

    def test_a_judged_goal_stays_pending_until_verified(self):
        evaluated, violations, pending = goals.evaluate(self.ledger, None, "a prompt")
        self.assertEqual(evaluated[0]["verdict"], goals.VERDICT_PENDING)
        self.assertEqual(violations, [])
        self.assertEqual([goal["id"] for goal in pending], [self.ledger[0]["id"]])

    def test_a_verdict_is_applied_with_its_reason(self):
        _evaluated, _violations, pending = goals.evaluate(self.ledger, None, "a prompt")
        text = '{"verdicts":[{"id":"%s","met":false,"reason":"reads like an advert"}]}' % pending[0]["id"]
        verdicts = goals.parse_verification(text, pending)
        ledger, violations = goals.apply_verdicts(self.ledger, verdicts)
        self.assertEqual(ledger[0]["verdict"], goals.VERDICT_UNMET)
        self.assertEqual(len(violations), 1)
        self.assertIn("reads like an advert", violations[0])

    def test_an_unreadable_verdict_leaves_the_goal_pending_not_met(self):
        _evaluated, _violations, pending = goals.evaluate(self.ledger, None, "a prompt")
        for text in ("", "sure! it passes", "{oops", '{"verdicts": "yes"}'):
            self.assertEqual(goals.parse_verification(text, pending), {}, text)

    def test_a_skipped_goal_is_not_silently_marked_met(self):
        _evaluated, _violations, pending = goals.evaluate(self.ledger, None, "a prompt")
        verdicts = goals.parse_verification('{"verdicts":[{"id":"other","met":true}]}', pending)
        ledger, violations = goals.apply_verdicts(self.ledger, verdicts)
        self.assertEqual(ledger[0]["verdict"], goals.VERDICT_PENDING)
        self.assertEqual(violations, [])

    def test_a_fenced_answer_is_still_read(self):
        _evaluated, _violations, pending = goals.evaluate(self.ledger, None, "a prompt")
        text = '```json\n{"verdicts":[{"id":"%s","met":true,"reason":"fine"}]}\n```' % pending[0]["id"]
        self.assertTrue(goals.parse_verification(text, pending)[pending[0]["id"]][0])


class RenderTests(unittest.TestCase):
    def test_render_lists_only_enabled_goals(self):
        ledger = [goals.new_goal("one"), goals.new_goal("two")]
        ledger = goals.toggle(ledger, ledger[1]["id"], False)
        self.assertEqual(goals.render(ledger), "- one")

    def test_unmet_reports_only_enabled_failures(self):
        ledger = [goals.new_goal("one"), goals.new_goal("two")]
        ledger[0]["verdict"] = goals.VERDICT_UNMET
        ledger[1]["verdict"] = goals.VERDICT_UNMET
        ledger = goals.toggle(ledger, ledger[1]["id"], False)
        self.assertEqual([goal["text"] for goal in goals.unmet(ledger)], ["one"])


if __name__ == "__main__":
    unittest.main()
