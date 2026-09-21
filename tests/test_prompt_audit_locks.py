import unittest

from backend.prompt_audit import audit_prompt
from backend.prompt_repair import audit_failures
from backend.scene_bible import ORIGIN_ASSET, ORIGIN_INVENTED, new_bible, set_field


def locked_bible():
    b = new_bible()
    b = set_field(b, "subject", "Bob, a man in a faded denim jacket", ORIGIN_ASSET)
    return b


KEPT = "Bob sits quietly, his faded denim jacket open at the collar."
DROPPED = "A woman in a silk gown stands by the window."


class NonReferenceModeTests(unittest.TestCase):
    """Locks must be audited in every mode, not only Reference."""

    def test_lock_violation_detected_in_i2va(self):
        result = audit_prompt(DROPPED, mode="I2VA", bible=locked_bible())
        self.assertEqual(result["lock_violations"], ["subject"])
        self.assertTrue(result["repair_required"])

    def test_intact_locks_do_not_trigger_repair(self):
        result = audit_prompt(KEPT, mode="I2VA", bible=locked_bible())
        self.assertEqual(result["lock_violations"], [])
        self.assertFalse(result["repair_required"])

    def test_behaviour_unchanged_without_a_bible(self):
        result = audit_prompt(DROPPED, mode="T2VA")
        self.assertEqual(result["lock_violations"], [])
        self.assertFalse(result["repair_required"])
        self.assertIsNone(result["official_format_pass"])
        self.assertEqual(result["reference_understanding"], "not_applicable")

    def test_every_video_mode_is_covered(self):
        for mode in ("T2VA", "I2VA", "FL2VA", "L2VA"):
            with self.subTest(mode=mode):
                result = audit_prompt(DROPPED, mode=mode, bible=locked_bible())
                self.assertTrue(result["repair_required"], mode)


class ReferenceModeTests(unittest.TestCase):
    def _reference_prompt(self, body):
        return (
            "subject_definitions: <Subject 1> is a person.\n"
            "summary: [reference generation] A scene.\n"
            "retention_analysis: <Subject 1> fully_preserved.\n"
            f"detailed_description: [Shot 1] {body}\n"
            "overall_soundscape: Quiet room tone.\n"
            "non_diegetic_music: N/A\n"
        )

    def test_lock_violation_fails_official_format(self):
        result = audit_prompt(
            self._reference_prompt(DROPPED), mode="Reference", bible=locked_bible()
        )
        self.assertEqual(result["lock_violations"], ["subject"])
        self.assertTrue(result["repair_required"])
        self.assertFalse(result["official_format_pass"])

    def test_structurally_valid_prompt_still_passes_with_locks_intact(self):
        result = audit_prompt(
            self._reference_prompt(KEPT), mode="Reference", bible=locked_bible()
        )
        self.assertEqual(result["lock_violations"], [])


class RepairInstructionTests(unittest.TestCase):
    def test_failure_names_the_dropped_field(self):
        result = audit_prompt(DROPPED, mode="I2VA", bible=locked_bible())
        failures = audit_failures(result)
        self.assertEqual(len(failures), 1)
        self.assertIn("subject", failures[0])

    def test_no_failure_when_locks_hold(self):
        result = audit_prompt(KEPT, mode="I2VA", bible=locked_bible())
        self.assertEqual(audit_failures(result), [])

    def test_unlocked_fields_are_never_reported(self):
        b = set_field(new_bible(), "era", "1970s", ORIGIN_INVENTED)
        result = audit_prompt("A bare white room.", mode="I2VA", bible=b)
        self.assertEqual(result["lock_violations"], [])


if __name__ == "__main__":
    unittest.main()
