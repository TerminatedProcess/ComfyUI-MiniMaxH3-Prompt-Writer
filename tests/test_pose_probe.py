"""Reading a pose from evidence rather than asking for the pose.

The rule these tests encode was measured, not assumed: asked "standing or
sitting?" about a real reference the model said sitting, twice, including with a
fixed menu. Asked whether the chair seat was occupied and where the body was
relative to the desk edge, it got all of it right. So the model reports evidence
and this module draws the conclusion.
"""
from __future__ import annotations

import unittest

from backend import pose_probe


# The answers qwen3-vl-27b actually returned for the reference that started this.
REAL_STANDING = {
    "seat_in_frame": True,
    "seat_occupied": False,
    "lap_visible": False,
    "legs_upright": False,
    "body_vs_furniture": "in front of its edge",
    "camera_height": "eye level",
}


class DeriveTests(unittest.TestCase):
    def test_the_reference_that_was_read_as_sitting_derives_as_standing(self):
        pose, why = pose_probe.derive(REAL_STANDING)
        self.assertEqual(pose, "standing")
        self.assertIn("empty", why)
        self.assertEqual(pose_probe.phrase(pose, REAL_STANDING), "standing, in front of the desk edge")

    def test_an_occupied_seat_is_sitting(self):
        answers = {**REAL_STANDING, "seat_occupied": True, "body_vs_furniture": "behind it"}
        pose, _why = pose_probe.derive(answers)
        self.assertEqual(pose, "sitting")
        self.assertEqual(pose_probe.phrase(pose, answers), "sitting, on the seat, behind the desk")

    def test_a_visible_lap_is_sitting_even_with_no_seat_in_frame(self):
        answers = {"seat_in_frame": False, "seat_occupied": False, "lap_visible": True, "legs_upright": False}
        self.assertEqual(pose_probe.derive(answers)[0], "sitting")

    def test_seated_evidence_outranks_standing_evidence(self):
        """A positive observation beats an inference from an absence."""
        answers = {**REAL_STANDING, "lap_visible": True, "legs_upright": True}
        self.assertEqual(pose_probe.derive(answers)[0], "sitting")

    def test_nothing_visible_leaves_the_pose_unset_rather_than_guessing(self):
        answers = {
            "seat_in_frame": False, "seat_occupied": False, "lap_visible": False,
            "legs_upright": False, "body_vs_furniture": "none visible",
        }
        pose, why = pose_probe.derive(answers)
        self.assertIsNone(pose)
        self.assertIn("nothing in the picture", why)

    def test_two_people_in_frame_means_no_pose_is_read(self):
        """The evidence stops being about one body, so there is nothing to lock.

        A seat can be occupied by one person while another stands beside it:
        every answer below would then describe whichever body the model looked
        at, and the document holds one subject.
        """
        mixed = {
            "people_count": "more than one", "seat_in_frame": True, "seat_occupied": True,
            "lap_visible": True, "legs_upright": True, "body_vs_furniture": "behind it",
        }
        pose, why = pose_probe.derive(mixed)
        self.assertIsNone(pose)
        self.assertIn("more than one person", why)

    def test_an_empty_room_is_not_a_pose_either(self):
        pose, why = pose_probe.derive({"people_count": "none", "seat_in_frame": True, "seat_occupied": False})
        self.assertIsNone(pose)
        self.assertIn("no person", why)

    def test_one_person_is_read_as_before(self):
        answers = {**REAL_STANDING, "people_count": "one"}
        self.assertEqual(pose_probe.derive(answers)[0], "standing")

    def test_an_unreadable_answer_is_not_a_pose(self):
        for answer in (None, "sitting", {}, {"pose": "sitting"}, []):
            self.assertIsNone(pose_probe.derive(answer)[0], answer)

    def test_answers_outside_the_menu_are_discarded_not_coerced(self):
        answers = {**REAL_STANDING, "body_vs_furniture": "sort of near it", "camera_height": "low"}
        normalized = pose_probe.normalize(answers)
        self.assertNotIn("body_vs_furniture", normalized)
        self.assertNotIn("camera_height", normalized)
        # The remaining evidence still decides it.
        self.assertEqual(pose_probe.derive(answers)[0], "standing")

    def test_yes_and_no_strings_are_read_as_booleans(self):
        answers = {**REAL_STANDING, "seat_occupied": "yes"}
        self.assertEqual(pose_probe.derive(answers)[0], "sitting")

    def test_the_camera_height_is_only_said_when_it_is_not_eye_level(self):
        low = {**REAL_STANDING, "camera_height": "below eye level"}
        self.assertEqual(pose_probe.phrase("standing", low), "standing, in front of the desk edge, camera below eye level")

    def test_a_build_phrasing_and_a_derived_word_are_the_same_fact(self):
        self.assertTrue(pose_probe.agrees("standing", "Standing, three-quarter to the camera, arm extended"))
        self.assertTrue(pose_probe.agrees("sitting", "Sitting at her desk on a wooden chair"))
        # Near-enough readings are not worth interrupting the user over.
        self.assertTrue(pose_probe.agrees("standing", "Leaning against the desk edge"))
        self.assertTrue(pose_probe.agrees("sitting", "Perched on the desk"))

    def test_a_real_contradiction_does_not_count_as_agreement(self):
        self.assertFalse(pose_probe.agrees("standing", "Sitting at her desk"))
        self.assertFalse(pose_probe.agrees("sitting", "Standing in front of the desk"))
        self.assertFalse(pose_probe.agrees("standing", "at her desk"), "no pose word is not agreement")

    def test_the_probe_never_names_a_pose_to_the_model(self):
        """The whole point: asking for the conclusion is what got it wrong."""
        asked = " ".join(pose_probe.QUESTIONS.values()).lower() + pose_probe.schema_line().lower()
        for word in ("standing", "sitting", "kneeling", "crouching", "posture", "pose"):
            self.assertNotIn(word, asked, word)


if __name__ == "__main__":
    unittest.main()
