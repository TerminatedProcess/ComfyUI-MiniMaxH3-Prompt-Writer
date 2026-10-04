"""Relations: how things sit against each other, as edges rather than prose.

Fields say what each thing IS. "In front of the desk" never said from whose
side, and "B on A" shares every token with "A on B" -- so an arrangement cannot
live in a string and cannot be audited by counting words. It lives as an edge
with a closed vocabulary, and a locked edge is checked by the verifier.
"""
from __future__ import annotations

import unittest

from backend import conversation, generic, goals
from backend.targets import base


def peopled():
    doc = generic.new_doc()
    doc = generic.set_field(doc, "subject", "a blonde woman", generic.ORIGIN_USER)
    doc = generic.set_field(doc, "subject#2", "a brunette", generic.ORIGIN_USER)
    return doc


class EdgeTests(unittest.TestCase):
    def test_the_vocabulary_is_closed(self):
        with self.assertRaises(generic.GenericError):
            generic.validate_edge({"from": "A", "rel": "vibing with", "to": "the couch"})

    def test_an_endpoint_is_a_person_or_a_noun(self):
        edge = generic.validate_edge({"from": "b", "rel": "on", "to": "A"})
        self.assertEqual((edge["from"], edge["to"]), ("B", "A"), "a person letter is normalised")
        self.assertEqual(generic.endpoint_person("the couch"), None)
        self.assertEqual(generic.endpoint_person("B"), 2)
        with self.assertRaises(generic.GenericError):
            generic.validate_edge({"from": "A", "rel": "on", "to": "A"})

    def test_a_relation_reads_as_a_person_would_say_it(self):
        doc = generic.set_edges(peopled(), [
            {"from": "A", "rel": "on", "to": "the couch", "origin": "user"},
            {"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap", "origin": "user"},
            {"from": "A", "rel": "occludes", "to": "the desk", "origin": "asset"},
        ])
        self.assertEqual(
            generic.render_relations(doc),
            "A on the couch; B on A (across her lap); A occludes the desk",
        )
        self.assertIn("Relations\nA on the couch;", generic.render(doc))

    def test_the_same_relation_twice_is_stored_once(self):
        doc = generic.set_edges(peopled(), [
            {"from": "A", "rel": "on", "to": "the couch", "origin": "asset"},
            {"from": "A", "rel": "on", "to": "the couch", "origin": "user"},
        ])
        self.assertEqual(len(generic.edges(doc)), 1)
        self.assertEqual(generic.edges(doc)[0]["origin"], "user", "the later claim wins")


class BuildTests(unittest.TestCase):
    ANSWER = (
        '{"scene": {"subject": "a blonde woman", "subject#2": "a brunette"},'
        ' "relations": [{"from": "A", "rel": "on", "to": "the couch"},'
        ' {"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap"},'
        ' {"from": "A", "rel": "levitating over", "to": "the rug"}]}'
    )

    def test_a_build_with_media_records_what_it_saw_as_locked(self):
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(), self.ANSWER, brief="two women on a couch", has_media=True,
        )
        self.assertEqual(len(generic.edges(doc)), 2, "the invented relation was dropped, not fatal")
        self.assertEqual(len(generic.locked_edges(doc)), 2)
        self.assertIn("relations", changed)

    def test_without_media_an_arrangement_is_a_proposal_not_a_fact(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(), self.ANSWER, brief="two women on a couch", has_media=False,
        )
        self.assertEqual(len(generic.edges(doc)), 2)
        self.assertEqual(generic.locked_edges(doc), (), "nothing was observed, so nothing is enforced")

    def test_a_relation_the_user_fixed_survives_a_rebuild(self):
        doc = generic.set_edges(peopled(), [
            {"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap", "origin": "user"},
        ])
        rebuilt, _changed, _protected = conversation.apply_build(
            doc,
            '{"scene": {"location": "a front room"},'
            ' "relations": [{"from": "A", "rel": "on", "to": "the couch"}]}',
            brief="", has_media=True,
        )
        texts = generic.render_relations(rebuilt)
        self.assertIn("B on A (across her lap)", texts, "the user's arrangement is not re-observed away")
        self.assertIn("A on the couch", texts)


class EnforcementTests(unittest.TestCase):
    def doc(self):
        return generic.set_edges(peopled(), [
            {"from": "B", "rel": "on", "to": "A", "qualifier": "across her lap", "origin": "user"},
            {"from": "A", "rel": "occludes", "to": "the desk", "origin": "invented"},
        ])

    def test_a_locked_relation_becomes_a_judged_goal_naming_both_people(self):
        derived = goals.from_edges(self.doc())
        self.assertEqual(len(derived), 1, "only the locked one is enforced")
        goal = derived[0]
        self.assertEqual(goal["kind"], goals.KIND_JUDGED)
        self.assertIn("a brunette (person B)", goal["text"])
        self.assertIn("a blonde woman (person A)", goal["text"])

    def test_a_named_person_is_checked_by_name(self):
        doc = generic.ensure_names(self.doc(), seed="session-1")
        named = generic.names(doc)
        goal = goals.from_edges(doc)[0]
        self.assertIn(named[1], goal["text"])
        self.assertIn(named[2], goal["text"])
        self.assertIn("across her lap", goal["text"])

    def test_the_compile_checks_relations_without_them_being_stored_goals(self):
        assembled = {"input": {"generic": self.doc(), "goals": []}}
        result = base.shared_checks(assembled, "Two women sit side by side on a couch.")
        self.assertTrue(result["goals"], "the derived goal reached the audit")
        self.assertIn("edge:B:on:A", [goal["id"] for goal in result["goals"]])
        # Judged goals need a verifier pass, so the compile reports it pending
        # rather than quietly passing a relation nobody checked.
        self.assertIn("edge:B:on:A", result["goals_pending"])

    def test_what_must_not_appear_is_checked_as_a_negative(self):
        """Measured: the token audit asked for the opposite of what it meant.

        It demanded that "No visible props, furniture, or scenery" be PRESENT in
        the prose, and reported the failure as "Must not appear must be: No
        visible props". Prose that obeys the rule never contains that phrase,
        and prose that breaks it ("mirrors and barres along the walls") does not
        contain it either -- so the check could only ever be wrong.
        """
        doc = generic.set_field(
            generic.new_doc(), "exclusions", "No visible props, furniture, or scenery", generic.ORIGIN_USER,
        )
        self.assertEqual(generic.lock_violations(doc, "a bare studio, nothing else in frame"), ("exclusions",),
                         "the document still tracks it")
        assembled = {"input": {"generic": doc, "goals": [], "mode": "Krea2"}}
        result = base.shared_checks(assembled, "a bare studio, nothing else in frame")
        self.assertNotIn("exclusions", result["lock_violations"], "but the compile is not audited for it by words")
        self.assertIn("exclusions", [goal["id"] for goal in result["goals"]])
        self.assertIn("must not include", [goal["text"] for goal in result["goals"] if goal["id"] == "exclusions"][0])

    def test_derived_goals_never_join_the_user_s_standing_ledger(self):
        """Observed live: every compile added another copy of every relation.

        The compile writes its evaluated goal list back to the session, and the
        derived ones rode along -- so the ledger grew by three edges per run and
        the verifier re-checked each of them again and again.
        """
        doc = generic.set_edges(peopled(), [{"from": "B", "rel": "on", "to": "A", "origin": "user"}])
        evaluated = [*goals.from_edges(doc), *goals.from_exclusions(doc), goals.new_goal("never mention a brand")]
        kept = goals.without_derived(evaluated)
        self.assertEqual([goal["text"] for goal in kept], ["never mention a brand"])

    def test_a_document_with_only_relations_is_not_empty(self):
        doc = generic.set_edges(generic.new_doc(), [
            {"from": "A", "rel": "on", "to": "the couch", "origin": "user"},
        ])
        self.assertFalse(generic.is_empty(doc))


if __name__ == "__main__":
    unittest.main()
