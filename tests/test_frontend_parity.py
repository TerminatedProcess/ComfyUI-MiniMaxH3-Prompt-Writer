"""The one rule that exists in both Python and JavaScript must stay one rule.

The studio relabels the delivery bar the moment media changes, so it cannot wait
for a round trip to learn H3's inferred mode -- the table is duplicated in
`web/mode_inference.js`. This parses that file and holds it to the Python
implementation, so the copy cannot drift in silence.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from backend.targets.h3 import infer_mode

MODULE = Path(__file__).resolve().parents[1] / "web" / "mode_inference.js"


class ModeInferenceParityTests(unittest.TestCase):
    def setUp(self):
        self.source = MODULE.read_text(encoding="utf-8")

    def js_table(self) -> list[str]:
        match = re.search(r"INFERRED_BY_IMAGE_COUNT\s*=\s*(\[[^\]]*\])", self.source)
        self.assertIsNotNone(match, "INFERRED_BY_IMAGE_COUNT is no longer a literal array")
        return json.loads(match.group(1).replace("'", '"'))

    def js_clip_mode(self) -> str:
        match = re.search(r'INFERRED_WITH_CLIPS\s*=\s*"([A-Za-z0-9]+)"', self.source)
        self.assertIsNotNone(match, "INFERRED_WITH_CLIPS is no longer a literal string")
        return match.group(1)

    def test_the_image_count_table_matches_python(self):
        table = self.js_table()
        self.assertEqual(table, [infer_mode(count) for count in range(len(table))])

    def test_the_clip_rule_matches_python(self):
        clip_mode = self.js_clip_mode()
        self.assertEqual(clip_mode, infer_mode(0, video_count=1))
        self.assertEqual(clip_mode, infer_mode(0, audio_count=1))
        self.assertEqual(clip_mode, infer_mode(len(self.js_table())))

    def test_l2va_is_never_inferred_on_either_side(self):
        self.assertNotIn("L2VA", self.js_table())
        self.assertNotIn("L2VA", {infer_mode(count) for count in range(6)})


if __name__ == "__main__":
    unittest.main()


SUBJECTS_MODULE = Path(__file__).resolve().parents[1] / "web" / "media_subjects.js"


class SubjectCeilingParityTests(unittest.TestCase):
    """The studio allocates subject boxes without asking the server first.

    `Add subject` has to know when there is no room for another person, so the
    ceiling is duplicated in `web/media_subjects.js`. A studio that offers a
    seventh box would only find out it was wrong at the next drop.
    """

    def test_the_subject_ceiling_matches_python(self):
        from backend.generic import MAX_PEOPLE

        source = SUBJECTS_MODULE.read_text(encoding="utf-8")
        match = re.search(r"MAX_SUBJECTS\s*=\s*(\d+)", source)
        self.assertIsNotNone(match, "MAX_SUBJECTS is no longer a literal number")
        self.assertEqual(int(match.group(1)), MAX_PEOPLE)

    def test_the_letters_themselves_match_python(self):
        """Length alone passed for ["1","2",...].

        A changed PERSON_LETTERS would then leave the studio labelling boxes
        A, B, C while the document called the same people something else --
        silently, which is the one thing this harness exists to prevent.
        """
        from backend.generic import PERSON_LETTERS

        source = SUBJECTS_MODULE.read_text(encoding="utf-8")
        match = re.search(r"LETTERS\s*=\s*(\[[^\]]*\])", source)
        self.assertIsNotNone(match, "LETTERS is no longer a literal array")
        self.assertEqual(json.loads(match.group(1).replace("'", '"')), list(PERSON_LETTERS))
