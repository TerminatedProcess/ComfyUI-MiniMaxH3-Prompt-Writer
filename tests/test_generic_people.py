"""More than one person in the document.

The document used to describe exactly one, so two people shared one `wardrobe`
string and nothing recorded whose the red dress was. A person is now a key
suffix -- `wardrobe` is A's, `wardrobe#2` is B's -- which keeps every consumer
(locks, audit, goals, conversation patches, UI rows) addressing fields by name.
"""
from __future__ import annotations

import unittest

from backend import conversation, generic


def two_people():
    doc = generic.new_doc()
    for key, value in (
        ("subject", "a blonde woman"), ("wardrobe", "a red dress"), ("pose", "sitting on the couch"),
        ("subject#2", "a brunette"), ("wardrobe#2", "jeans"), ("pose#2", "lying across A's lap"),
    ):
        doc = generic.set_field(doc, key, value, generic.ORIGIN_USER)
    return doc


class KeyTests(unittest.TestCase):
    def test_person_one_keeps_the_bare_field_names(self):
        """Every existing session stays valid, and single-subject work is unchanged."""
        self.assertEqual(generic.person_key("wardrobe", 1), "wardrobe")
        self.assertEqual(generic.person_key("wardrobe", 2), "wardrobe#2")
        self.assertEqual(generic.people(generic.new_doc()), (1,))
        self.assertEqual(generic.doc_fields(generic.new_doc()), generic.FIELDS)

    def test_only_subject_fields_repeat(self):
        self.assertTrue(generic.is_field("wardrobe#2"))
        self.assertFalse(generic.is_field("location#2"), "a scene has one location")
        self.assertFalse(generic.is_field("wardrobe#1"), "person one is never suffixed")
        self.assertFalse(generic.is_field(f"wardrobe#{generic.MAX_PEOPLE + 1}"))
        self.assertFalse(generic.is_field("wardrobe#x"))
        self.assertFalse(generic.is_field("wardrobe#"))

    def test_a_second_person_adds_keys_in_reading_order(self):
        keys = generic.doc_fields(two_people())
        self.assertEqual(keys[:6], generic.PERSON_FIELDS)
        self.assertEqual(keys[6:12], tuple(f"{field}#2" for field in generic.PERSON_FIELDS))
        self.assertEqual(keys[12], "location", "the scene follows the people")

    def test_an_audit_message_says_whose_wardrobe(self):
        self.assertEqual(generic.label_for("wardrobe"), "Wardrobe")
        self.assertEqual(generic.label_for("wardrobe#2"), "Wardrobe (B)")

    def test_the_groups_name_the_people_only_when_there_are_several(self):
        self.assertEqual(generic.doc_groups(generic.new_doc())[0][0], "Subject")
        titles = [title for title, _keys in generic.doc_groups(two_people())]
        self.assertEqual(titles[:2], ["Subject A", "Subject B"])
        self.assertIn("Scene", titles)


class DocumentTests(unittest.TestCase):
    def test_both_people_render_as_their_own_block(self):
        rendered = generic.render(two_people())
        self.assertIn("Subject A\nSubject: a blonde woman", rendered)
        self.assertIn("Subject B\nSubject: a brunette", rendered)
        self.assertIn("Pose: lying across A's lap", rendered)

    def test_every_person_is_locked_independently(self):
        doc = two_people()
        self.assertEqual(
            generic.locked_fields(doc),
            ("subject", "wardrobe", "pose", "subject#2", "wardrobe#2", "pose#2"),
        )
        # Whose fact went missing is now answerable.
        missing = generic.lock_violations(doc, "A blonde woman in a red dress sits alone on the couch.")
        self.assertIn("subject#2", missing)
        self.assertNotIn("subject", missing)

    def test_the_constraints_block_credits_each_person(self):
        block = generic.render_constraints(two_people())
        self.assertIn("- Wardrobe: a red dress", block)
        self.assertIn("- Wardrobe (B): jeans", block)

    def test_a_patch_may_introduce_a_person_the_document_did_not_have(self):
        doc, changed, _protected = generic.apply_patch(
            generic.new_doc(), {"subject#2": "a man in a grey suit"}, source=generic.SOURCE_USER,
        )
        self.assertEqual(changed, ("subject#2",))
        self.assertEqual(generic.people(doc), (1, 2))

    def test_a_patch_naming_an_impossible_person_is_refused(self):
        with self.assertRaises(generic.GenericError):
            generic.apply_patch(generic.new_doc(), {"subject#99": "a crowd"}, source=generic.SOURCE_USER)


class NameTests(unittest.TestCase):
    """People get a handle a person can say out loud.

    "B's wardrobe" is a database row; "Bob's tights" is a sentence. The letters
    stay the machine's handle -- relations and the audit use them -- but the
    name is what the user and the model talk in.
    """

    def peopled(self):
        doc = generic.new_doc()
        doc = generic.set_field(doc, "subject", "a woman with curled brown hair", generic.ORIGIN_ASSET)
        doc = generic.set_field(doc, "subject#2", "a man in his 30s", generic.ORIGIN_ASSET)
        return generic.ensure_names(doc, seed="session-1")

    def test_a_woman_and_a_man_get_names_that_fit(self):
        from backend import person_names
        given = generic.names(self.peopled())
        self.assertIn(given[1], person_names.FEMININE)
        self.assertIn(given[2], person_names.MASCULINE)

    def test_an_unreadable_description_takes_a_neutral_name(self):
        """A wrong name is stickier than a wrong letter: do not guess."""
        from backend import person_names
        doc = generic.set_field(generic.new_doc(), "subject", "a figure in a heavy coat", generic.ORIGIN_ASSET)
        self.assertIn(generic.names(generic.ensure_names(doc, seed="s"))[1], person_names.NEUTRAL)

    def test_names_are_stable_across_rebuilds(self):
        doc = self.peopled()
        again = generic.ensure_names(doc, seed="session-1")
        self.assertEqual(generic.names(doc), generic.names(again))

    def test_two_people_never_share_a_name(self):
        doc = generic.new_doc()
        for index in range(1, 5):
            doc = generic.set_field(doc, generic.person_key("subject", index), "a woman", generic.ORIGIN_ASSET)
        given = generic.names(generic.ensure_names(doc, seed="s"))
        self.assertEqual(len(set(given.values())), len(given))

    def test_an_empty_person_is_not_named(self):
        self.assertEqual(generic.names(generic.ensure_names(generic.new_doc(), seed="s")), {})

    def test_the_name_shows_in_the_heading_and_in_audit_messages(self):
        doc = self.peopled()
        name = generic.names(doc)[2]
        self.assertIn(f"{name} (B)", [title for title, _keys in generic.doc_groups(doc)])
        self.assertEqual(generic.label_for("wardrobe#2", doc), f"Wardrobe ({name})")

    def test_the_user_can_rename_anyone(self):
        doc = generic.set_name(self.peopled(), 1, "Elke")
        self.assertEqual(generic.names(doc)[1], "Elke")
        self.assertNotIn(1, generic.names(generic.set_name(doc, 1, "")), "clearing a name removes it")

    def test_the_compile_is_told_the_names_are_not_in_the_picture(self):
        rendered = generic.render(self.peopled())
        self.assertIn("are labels for these people, not part of the scene", rendered)
        self.assertIn("Never write a label in the prompt", rendered)


class BuildTests(unittest.TestCase):
    def test_a_build_can_write_both_people(self):
        answer = (
            '{"scene": {"subject": "a blonde woman", "wardrobe": "a red dress",'
            ' "pose": "sitting on the couch", "subject#2": "a brunette", "wardrobe#2": "jeans",'
            ' "pose#2": "lying across A\'s lap"}}'
        )
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(), answer, brief="two women on a couch", has_media=False,
        )
        self.assertEqual(generic.record(doc, "wardrobe#2")["value"], "jeans")
        self.assertIn("pose#2", changed)
        # A: red dress. B: jeans. The binding is in the document, not in a sentence.
        self.assertEqual(generic.record(doc, "wardrobe")["value"], "a red dress")

    def test_the_contract_asks_for_one_object_per_person(self):
        """Measured, not chosen: a flat key list let the model mix people up.

        Given a two-dancer picture and suffixed keys it wrote the woman as A and
        then put the MAN's tights in A's wardrobe. An object per person makes
        the binding syntactic rather than a rule to remember.
        """
        for contract in (conversation.BUILD_INSTRUCTIONS, conversation.TURN_INSTRUCTIONS):
            self.assertIn("NEVER describe two people in one field", contract)
        self.assertIn('"people"', conversation.BUILD_INSTRUCTIONS)
        self.assertIn('"wardrobe": "white tights, no shirt"', conversation.BUILD_INSTRUCTIONS)

    def test_a_people_list_lands_on_the_per_person_keys(self):
        answer = (
            '{"scene": {"location": "a studio"}, "people": ['
            '{"subject": "a woman", "wardrobe": "a white leotard", "pose": "curled up"},'
            '{"subject": "a man", "wardrobe": "white tights", "pose": "kneeling"}]}'
        )
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(), answer, brief="two dancers", has_media=True,
        )
        self.assertEqual(generic.record(doc, "wardrobe")["value"], "a white leotard")
        self.assertEqual(generic.record(doc, "wardrobe#2")["value"], "white tights")
        self.assertEqual(generic.record(doc, "wardrobe")["origin"], generic.ORIGIN_ASSET,
                         "read off the picture, so it locks")

    def test_suffixed_keys_sent_directly_still_work(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            '{"scene": {"subject": "a woman", "subject#2": "a man"}}',
            brief="two dancers", has_media=False,
        )
        self.assertEqual(generic.record(doc, "subject#2")["value"], "a man")


if __name__ == "__main__":
    unittest.main()
