"""Build and steer the generic prompt: origin assignment and the chat contract.

Origins are assigned structurally here, never by asking the model to classify
its own inputs -- that was measured at 0/6 on this stack and erred toward
marking the user's own words as invented, which would let them be overwritten.
"""
from __future__ import annotations

import json
import unittest

from backend import conversation, generic, goals


def build_answer(observed=None, scene=None, from_brief=None):
    payload = {"observed": observed or {}, "scene": scene or {}}
    if from_brief:
        payload["from_brief"] = from_brief
    return json.dumps(payload)


class BuildOriginTests(unittest.TestCase):
    def test_a_fact_seen_in_the_reference_becomes_an_asset_fact(self):
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(observed={"wardrobe": "a red pleated skirt"}, scene={"wardrobe": "a red pleated skirt"}),
            brief="a woman on a bridge",
            has_media=True,
        )
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_ASSET)
        self.assertEqual(record["observed"], "a red pleated skirt")
        self.assertIn("wardrobe", changed)

    def test_a_fact_the_brief_states_becomes_the_users_words(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"location": "a rain-soaked tram stop at blue hour"}),
            brief="a solitary character waits at a rain-soaked tram stop at blue hour",
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "location")["origin"], generic.ORIGIN_USER)

    def test_a_fact_nobody_stated_is_marked_invented(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"lighting": "a single sodium streetlight raking across wet tarmac"}),
            brief="a woman waits for a tram",
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "lighting")["origin"], generic.ORIGIN_INVENTED)
        self.assertEqual(generic.locked_fields(doc), ())

    def test_a_scene_value_contradicting_the_reference_becomes_an_override_when_the_brief_says_so(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(observed={"wardrobe": "a blue skirt"}, scene={"wardrobe": "a red skirt"}),
            brief="same woman but make it a red skirt",
            has_media=True,
        )
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_OVERRIDE)
        self.assertEqual(record["observed"], "a blue skirt")

    def test_an_unsupported_deviation_defers_to_the_reference(self):
        """If nobody asked for the change, the picture wins."""
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(observed={"wardrobe": "a blue skirt"}, scene={"wardrobe": "a green tracksuit"}),
            brief="a woman on a bridge",
            has_media=True,
        )
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["value"], "a blue skirt")
        self.assertEqual(record["origin"], generic.ORIGIN_ASSET)

    def test_a_rebuild_does_not_overwrite_what_the_user_settled(self):
        doc = generic.set_field(generic.new_doc(), "wardrobe", "a red skirt", generic.ORIGIN_USER)
        doc, changed, protected = conversation.apply_build(
            doc,
            build_answer(scene={"wardrobe": "a beige trench coat", "mood": "wistful"}),
            brief="a woman on a bridge",
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "wardrobe")["value"], "a red skirt")
        self.assertIn("wardrobe", protected)
        self.assertIn("mood", changed)

    def test_placeholder_values_are_dropped_rather_than_stored(self):
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"weather": "unknown", "era": "not specified", "mood": "tense"}),
            brief="a tense scene",
            has_media=False,
        )
        self.assertEqual(changed, ("mood",))
        self.assertIn("weather", generic.unspecified_fields(doc))

    def test_observations_are_ignored_when_no_media_is_attached(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(observed={"wardrobe": "a red skirt"}, scene={"mood": "calm"}),
            brief="a calm scene",
            has_media=False,
        )
        self.assertIn("wardrobe", generic.unspecified_fields(doc))

    def test_an_unreadable_answer_is_an_error_not_an_empty_document(self):
        for text in ("", "Sure! Here is the scene.", "{broken"):
            with self.assertRaises(conversation.ConversationError):
                conversation.apply_build(generic.new_doc(), text, brief="x", has_media=False)

    def test_an_answer_with_no_fields_is_an_error(self):
        with self.assertRaises(conversation.ConversationError):
            conversation.apply_build(generic.new_doc(), build_answer(), brief="x", has_media=False)

    def test_a_fenced_answer_is_accepted(self):
        text = "```json\n" + build_answer(scene={"mood": "tense"}) + "\n```"
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(), text, brief="a tense scene", has_media=False
        )
        self.assertEqual(changed, ("mood",))


class BriefCreditTests(unittest.TestCase):
    """An expanded fact the user actually asked for is still the user's.

    Without the verified quote the writer files it as invented, and an invented
    fact is not locked -- so the courier the user asked for can quietly become
    someone else on the next compile.
    """

    BRIEF = (
        "At blue hour, a bicycle courier arrives at a quiet rooftop greenhouse, sets down a softly "
        "glowing parcel and watches the city lights switch on below."
    )
    EXPANDED = (
        "An adult male bicycle courier in his early 30s with short wind-tousled hair, straddling a "
        "matte-black city bike with a weathered satchel across his back"
    )

    def test_an_expanded_fact_with_a_real_quote_is_credited_to_the_user(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"subject": self.EXPANDED}, from_brief={"subject": "a bicycle courier"}),
            brief=self.BRIEF,
            has_media=False,
        )
        record = generic.record(doc, "subject")
        self.assertEqual(record["origin"], generic.ORIGIN_USER)
        self.assertIn("subject", generic.locked_fields(doc))

    def test_a_quote_the_user_never_wrote_does_not_credit_an_invention(self):
        """A claimed quote is checked against the brief, never taken on trust."""
        invented = "A weathered ship's captain in oilskins, lit by a swinging lantern"
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"subject": invented}, from_brief={"subject": "a ship's captain"}),
            brief=self.BRIEF,
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "subject")["origin"], generic.ORIGIN_INVENTED)
        self.assertEqual(generic.locked_fields(doc), ())

    def test_a_field_sharing_only_one_word_with_the_brief_is_not_locked(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"camera": "A slow push-in during the blue dusk"}),
            brief=self.BRIEF,
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "camera")["origin"], generic.ORIGIN_INVENTED)

    def test_an_expanded_field_keeping_the_users_phrase_is_credited_without_a_quote(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(scene={"location": "A quiet rooftop greenhouse of misted glass above the city"}),
            brief=self.BRIEF,
            has_media=False,
        )
        self.assertEqual(generic.record(doc, "location")["origin"], generic.ORIGIN_USER)

    def test_a_quote_is_matched_despite_whitespace_and_case(self):
        self.assertTrue(conversation.quoted_from_brief("A Bicycle   Courier", self.BRIEF))
        self.assertFalse(conversation.quoted_from_brief("a", self.BRIEF))
        self.assertFalse(conversation.quoted_from_brief(None, self.BRIEF))

    def test_a_quoted_deviation_from_the_image_still_becomes_an_override(self):
        doc, _changed, _protected = conversation.apply_build(
            generic.new_doc(),
            build_answer(
                observed={"wardrobe": "a blue skirt"},
                scene={"wardrobe": "a red skirt with a pleated hem"},
                from_brief={"wardrobe": "make it a red skirt"},
            ),
            brief="same woman, but make it a red skirt",
            has_media=True,
        )
        record = generic.record(doc, "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_OVERRIDE)
        self.assertEqual(record["observed"], "a blue skirt")


class TruncatedAnswerTests(unittest.TestCase):
    """A small local model stops mid-object; the finished fields still count.

    Measured at roughly one answer in three against the local abliterated
    qwen3-vl-8b with a 19-field document. Throwing all of it away costs the user
    another round trip for fields the model already wrote correctly.
    """

    TRUNCATED = (
        '{"observed": {}, "scene": {"subject": "A bicycle courier in his early 30s", '
        '"location": "A quiet rooftop greenhouse", "era": "Contemporary", "time_of_day": "Blue hour", '
        '"weather": "Cool and dry", "action": "He sets down a glowing parcel and wat'
    )

    def test_the_complete_fields_of_a_truncated_answer_survive(self):
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(), self.TRUNCATED, brief="a bicycle courier on a rooftop", has_media=False,
        )
        self.assertEqual(set(changed), {"subject", "location", "era", "time_of_day", "weather"})
        self.assertEqual(generic.record(doc, "subject")["value"], "A bicycle courier in his early 30s")
        self.assertIn("action", generic.unspecified_fields(doc))

    def test_a_document_cut_after_one_field_is_treated_as_a_failure(self):
        """Salvage is a rescue, not a result: a near-empty card is worse than a retry."""
        cut = '{"observed": {}, "scene": {"subject": "A bicycle courier in his early 30s", "appearance": "He has a we'
        with self.assertRaises(conversation.ConversationError) as raised:
            conversation.apply_build(generic.new_doc(), cut, brief="a bicycle courier", has_media=False)
        self.assertEqual(raised.exception.details["reason"], "truncated_early")
        self.assertEqual(raised.exception.details["recovered_fields"], 1)

    def test_a_complete_answer_is_never_second_guessed_for_being_short(self):
        short = '{"observed": {}, "scene": {"subject": "A bicycle courier", "mood": "quiet"}}'
        doc, changed, _protected = conversation.apply_build(
            generic.new_doc(), short, brief="a bicycle courier", has_media=False,
        )
        self.assertEqual(set(changed), {"subject", "mood"})

    def test_a_truncated_answer_with_no_complete_field_is_still_an_error(self):
        with self.assertRaises(conversation.ConversationError) as raised:
            conversation.apply_build(
                generic.new_doc(), '{"observed": {}, "scene": {"subject": "A bicycle cour',
                brief="x", has_media=False,
            )
        self.assertEqual(raised.exception.code, "INVALID_GENERIC_BUILD")

    def test_the_error_carries_the_models_own_words(self):
        with self.assertRaises(conversation.ConversationError) as raised:
            conversation.apply_build(generic.new_doc(), "I cannot help with that.", brief="x", has_media=False)
        self.assertIn("I cannot help", raised.exception.details["model_said"])
        self.assertEqual(raised.exception.details["reason"], "no_json_object")

    def test_a_truncated_turn_still_applies_what_it_completed(self):
        doc = generic.set_field(generic.new_doc(), "wardrobe", "a blue skirt", generic.ORIGIN_ASSET)
        outcome = conversation.apply_turn(
            doc, [],
            '{"reply": "Red it is.", "patch": {"wardrobe": "a red skirt", "mood": "warm',
            has_media=False,
        )
        self.assertEqual(outcome["changed"], ("wardrobe",))
        self.assertEqual(outcome["reply"], "Red it is.")


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.doc = generic.set_field(
            generic.new_doc(), "wardrobe", "a blue skirt", generic.ORIGIN_ASSET, observed="a blue skirt"
        )

    def turn(self, payload, *, doc=None, ledger=(), has_media=True):
        return conversation.apply_turn(
            doc if doc is not None else self.doc,
            list(ledger),
            json.dumps(payload),
            has_media=has_media,
        )

    def test_a_stated_fact_patches_the_document_as_an_override(self):
        outcome = self.turn({"reply": "Red it is.", "patch": {"wardrobe": "a red skirt"}})
        record = generic.record(outcome["generic"], "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_OVERRIDE)
        self.assertEqual(outcome["changed"], ("wardrobe",))
        self.assertEqual(outcome["reply"], "Red it is.")

    def test_a_re_observation_corrects_the_document_from_the_media(self):
        outcome = self.turn({
            "reply": "Re-read the picture; the skirt is red.",
            "observed": {"wardrobe": "a red pleated skirt"},
        })
        record = generic.record(outcome["generic"], "wardrobe")
        self.assertEqual(record["origin"], generic.ORIGIN_ASSET)
        self.assertEqual(record["value"], "a red pleated skirt")

    def test_a_re_observation_reports_fields_it_refused_to_touch(self):
        doc = generic.set_field(
            generic.new_doc(), "wardrobe", "a red skirt", generic.ORIGIN_OVERRIDE, observed="a blue skirt"
        )
        outcome = self.turn({"observed": {"wardrobe": "a blue skirt"}}, doc=doc)
        self.assertEqual(outcome["protected"], ("wardrobe",))
        self.assertEqual(generic.record(outcome["generic"], "wardrobe")["value"], "a red skirt")

    def test_observations_are_ignored_when_the_media_is_gone(self):
        outcome = self.turn({"observed": {"wardrobe": "a red skirt"}}, has_media=False)
        self.assertEqual(outcome["changed"], ())
        self.assertEqual(generic.record(outcome["generic"], "wardrobe")["value"], "a blue skirt")

    def test_a_standing_goal_is_added_to_the_ledger(self):
        outcome = self.turn({
            "reply": "Noted.",
            "goals": [{
                "text": "always follow the clothing colours in the image",
                "kind": "field",
                "fields": ["wardrobe"],
            }],
        })
        self.assertEqual(len(outcome["goals"]), 1)
        self.assertEqual(outcome["goals"][0]["kind"], goals.KIND_FIELD)
        self.assertEqual(outcome["goals_added"], ("always follow the clothing colours in the image",))

    def test_an_uncheckable_field_goal_becomes_a_judged_goal(self):
        outcome = self.turn({"goals": [{"text": "follow the image", "kind": "field", "fields": []}]})
        self.assertEqual(outcome["goals"][0]["kind"], goals.KIND_JUDGED)

    def test_an_uncheckable_presence_goal_becomes_a_judged_goal(self):
        outcome = self.turn({"goals": [{"text": "name the shop", "kind": "presence", "must_include": []}]})
        self.assertEqual(outcome["goals"][0]["kind"], goals.KIND_JUDGED)

    def test_a_turn_can_both_patch_and_set_a_goal(self):
        outcome = self.turn({
            "patch": {"wardrobe": "a red skirt"},
            "goals": [{"text": "keep her skirt red", "kind": "presence", "must_include": ["red skirt"]}],
        })
        self.assertEqual(outcome["changed"], ("wardrobe",))
        self.assertEqual(len(outcome["goals"]), 1)

    def test_an_unknown_field_in_a_patch_is_ignored_not_fatal(self):
        outcome = self.turn({"patch": {"vibes": "good", "mood": "tense"}})
        self.assertEqual(outcome["changed"], ("mood",))

    def test_a_reply_is_supplied_when_the_model_omits_one(self):
        self.assertEqual(self.turn({"patch": {"mood": "tense"}})["reply"], "Updated.")
        self.assertEqual(self.turn({})["reply"], "Nothing to change.")

    def test_an_unreadable_turn_is_an_error(self):
        with self.assertRaises(conversation.ConversationError):
            conversation.apply_turn(self.doc, [], "I have updated the skirt for you.", has_media=True)


class StandingInstructionTests(unittest.TestCase):
    """"Always X" must outlive the turn that said it, even on a small model.

    Observed live: told "her skirt is red, and always keep her skirt red from now
    on", the local model patched the field and recorded no goal at all -- so the
    rule would have expired immediately and the next compile could drift back.
    """

    def setUp(self):
        self.doc = generic.set_field(generic.new_doc(), "wardrobe", "a blue skirt", generic.ORIGIN_ASSET)

    def turn(self, payload, message):
        return conversation.apply_turn(
            self.doc, [], json.dumps(payload), has_media=False, message=message,
        )

    def test_a_standing_phrase_becomes_a_goal_when_the_model_records_none(self):
        outcome = self.turn(
            {"reply": "Done.", "patch": {"wardrobe": "a red skirt"}},
            "her skirt is red, and always keep her skirt red from now on",
        )
        self.assertEqual(len(outcome["goals"]), 1)
        self.assertEqual(outcome["goals"][0]["kind"], goals.KIND_JUDGED)
        self.assertIn("always keep her skirt red", outcome["goals"][0]["text"])

    def test_the_models_own_goal_is_preferred_when_it_records_one(self):
        outcome = self.turn(
            {"goals": [{"text": "keep her skirt red", "kind": "presence", "must_include": ["red skirt"]}]},
            "always keep her skirt red",
        )
        self.assertEqual(len(outcome["goals"]), 1)
        self.assertEqual(outcome["goals"][0]["kind"], goals.KIND_PRESENCE)

    def test_an_ordinary_correction_does_not_become_a_goal(self):
        outcome = self.turn({"patch": {"wardrobe": "a red skirt"}}, "her skirt is red, not blue")
        self.assertEqual(outcome["goals"], [])


class PhraseCreditTests(unittest.TestCase):
    def test_an_inserted_adjective_does_not_lose_the_users_phrase(self):
        self.assertTrue(conversation.brief_supports(
            "A flowing cerulean blue silk skirt with vertical pleats",
            "a woman in a blue skirt waits on a bridge",
        ))

    def test_distant_words_are_not_treated_as_a_phrase(self):
        self.assertFalse(conversation.brief_supports(
            "Blue hour light rakes across the wet stone, and a skirt of shadow falls over the parapet",
            "a woman in a blue skirt waits on a bridge",
        ))


class InstructionTests(unittest.TestCase):
    def test_build_instructions_follow_the_story_flag(self):
        self.assertIn("Story builder is ON", conversation.build_instructions(nsfw=False, story=True))
        self.assertIn("Story builder is OFF", conversation.build_instructions(nsfw=False, story=False))

    def test_no_audio_keeps_sound_out_of_the_document(self):
        text = conversation.build_instructions(nsfw=False, story=True, no_audio=True)
        self.assertIn("leave dialogue, soundscape and music out", text)
        self.assertNotIn("leave dialogue, soundscape and music out",
                         conversation.build_instructions(nsfw=False, story=True))
        self.assertIn("leave dialogue, soundscape and music out",
                      conversation.turn_instructions(nsfw=False, story=False, no_audio=True))

    def test_naughty_permission_is_only_present_when_set(self):
        self.assertIn("Adult or explicit content is permitted", conversation.build_instructions(nsfw=True, story=True))
        self.assertNotIn("Adult or explicit", conversation.build_instructions(nsfw=False, story=True))

    def test_turn_instructions_distinguish_a_goal_from_a_patch(self):
        text = conversation.turn_instructions(nsfw=False, story=False)
        self.assertIn("A one-off fact correction is a patch, NOT a goal", text)

    def test_brief_support_needs_most_of_the_words_present(self):
        self.assertTrue(conversation.brief_supports("a rain-soaked tram stop", "she waits at a rain-soaked tram stop"))
        self.assertFalse(conversation.brief_supports("a sodium streetlight on wet tarmac", "she waits for a tram"))


if __name__ == "__main__":
    unittest.main()
