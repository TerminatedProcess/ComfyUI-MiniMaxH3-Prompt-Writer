import unittest

from backend.intent import (
    IntentError,
    MAX_IMAGES,
    bible_from_resolution,
    describe_mode,
    infer_mode,
    shot_budget,
)
from backend.scene_bible import (
    FIELDS,
    ORIGIN_ASSET,
    ORIGIN_INVENTED,
    ORIGIN_USER,
    locked_fields,
)


def resolution(**overrides):
    base = {f: {"value": f"{f} value", "quote": None} for f in FIELDS}
    base.update(overrides)
    return base


class InferModeTests(unittest.TestCase):
    def test_counts_map_to_modes(self):
        self.assertEqual(infer_mode(0), "T2VA")
        self.assertEqual(infer_mode(1), "I2VA")
        self.assertEqual(infer_mode(2), "FL2VA")
        self.assertEqual(infer_mode(3), "Reference")
        self.assertEqual(infer_mode(9), "Reference")

    def test_never_infers_l2va(self):
        # One image cannot be distinguished from an I2VA first frame; guessing
        # "ends on this" would silently invert the user's intent.
        inferred = {infer_mode(n) for n in range(0, MAX_IMAGES + 1)}
        self.assertNotIn("L2VA", inferred)

    def test_video_or_audio_forces_reference(self):
        self.assertEqual(infer_mode(0, video_count=1), "Reference")
        self.assertEqual(infer_mode(1, audio_count=1), "Reference")

    def test_explicit_choice_wins(self):
        self.assertEqual(infer_mode(0, explicit="L2VA"), "L2VA")
        self.assertEqual(infer_mode(5, explicit="T2VA"), "T2VA")

    def test_rejects_unknown_explicit_mode(self):
        with self.assertRaises(IntentError) as ctx:
            infer_mode(0, explicit="Music3")
        self.assertEqual(ctx.exception.code, "UNKNOWN_MODE")

    def test_rejects_counts_over_the_h3_ceiling(self):
        with self.assertRaises(IntentError) as ctx:
            infer_mode(10)
        self.assertEqual(ctx.exception.code, "TOO_MANY_REFERENCES")
        with self.assertRaises(IntentError):
            infer_mode(0, video_count=4)

    def test_rejects_negative_counts(self):
        with self.assertRaises(IntentError) as ctx:
            infer_mode(-1)
        self.assertEqual(ctx.exception.code, "NEGATIVE_COUNT")


class DescribeModeTests(unittest.TestCase):
    def test_plain_english_for_every_inferable_mode(self):
        for n in (0, 1, 2, 3):
            self.assertTrue(describe_mode(infer_mode(n)))

    def test_avoids_jargon(self):
        for mode in ("T2VA", "I2VA", "FL2VA", "L2VA", "Reference"):
            self.assertNotIn("2VA", describe_mode(mode))


class ShotBudgetTests(unittest.TestCase):
    def test_matches_the_guide_table(self):
        self.assertEqual(shot_budget(5), (1, 2))
        self.assertEqual(shot_budget(8), (2, 3))
        self.assertEqual(shot_budget(15), (3, 5))

    def test_boundaries(self):
        self.assertEqual(shot_budget(6), (1, 2))
        self.assertEqual(shot_budget(10), (2, 3))
        self.assertEqual(shot_budget(10.5), (3, 5))

    def test_rejects_out_of_range(self):
        for bad in (3.9, 15.1):
            with self.assertRaises(IntentError) as ctx:
                shot_budget(bad)
            self.assertEqual(ctx.exception.code, "DURATION_OUT_OF_RANGE")

    def test_rejects_non_positive(self):
        with self.assertRaises(IntentError) as ctx:
            shot_budget(0)
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")


class BibleFromResolutionTests(unittest.TestCase):
    def test_builds_every_field(self):
        b = bible_from_resolution(resolution())
        self.assertEqual(set(b["fields"]), set(FIELDS))

    def test_missing_field_is_an_error(self):
        r = resolution()
        del r["weather"]
        with self.assertRaises(IntentError) as ctx:
            bible_from_resolution(r)
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")

    def test_empty_value_is_an_error(self):
        with self.assertRaises(IntentError) as ctx:
            bible_from_resolution(resolution(era={"value": "   ", "quote": None}))
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")

    def test_asset_fields_are_locked(self):
        b = bible_from_resolution(resolution(), asset_fields=("subject",))
        self.assertEqual(locked_fields(b), ("subject",))

    def test_asset_origin_beats_a_matching_quote(self):
        # Structural knowledge wins: the pipeline knows this came from an image.
        b = bible_from_resolution(
            resolution(subject={"value": "Bob", "quote": "Bob"}),
            asset_fields=("subject",),
            brief="Bob is sitting",
        )
        self.assertEqual(b["origins"]["subject"], ORIGIN_ASSET)

    def test_verified_quote_marks_user_origin(self):
        b = bible_from_resolution(
            resolution(location={"value": "his living room", "quote": "living room"}),
            brief="Bob is sitting in his living room",
        )
        self.assertEqual(b["origins"]["location"], ORIGIN_USER)

    def test_unverifiable_quote_downgrades_to_invented(self):
        # A hallucinated citation must not promote invented detail to user-stated.
        b = bible_from_resolution(
            resolution(era={"value": "1970s", "quote": "in the seventies"}),
            brief="Bob is sitting in his living room",
        )
        self.assertEqual(b["origins"]["era"], ORIGIN_INVENTED)

    def test_absent_quote_is_invented(self):
        b = bible_from_resolution(resolution(), brief="Bob is sitting")
        self.assertEqual(b["origins"]["weather"], ORIGIN_INVENTED)

    def test_plain_string_values_are_accepted(self):
        b = bible_from_resolution({f: f"{f} value" for f in FIELDS})
        self.assertEqual(b["origins"]["subject"], ORIGIN_INVENTED)

    def test_rejects_unknown_asset_field(self):
        with self.assertRaises(IntentError) as ctx:
            bible_from_resolution(resolution(), asset_fields=("vibe",))
        self.assertEqual(ctx.exception.code, "UNKNOWN_FIELD")

    def test_rejects_non_object_resolution(self):
        with self.assertRaises(IntentError) as ctx:
            bible_from_resolution(["subject"])
        self.assertEqual(ctx.exception.code, "INVALID_RESOLUTION")


if __name__ == "__main__":
    unittest.main()
