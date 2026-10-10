"""Binding a reference picture to a person.

The document has held one block per person for a while -- `wardrobe` is A's,
`wardrobe#2` is B's -- but nothing said which FILE belonged to which of them.
These tests cover the missing half: the binding on the asset, and the two rules
it must never break -- a Reference tag never moves, and nothing is thrown away.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import media
from backend.generic import MAX_PEOPLE


class SubjectBindingTests(unittest.TestCase):
    @staticmethod
    def asset(root: Path, asset_id: str, reference: str, kind: str = "image") -> dict:
        asset_dir = root / asset_id
        asset_dir.mkdir()
        original = asset_dir / {"image": "original.png", "video": "original.mp4", "audio": "original.wav"}[kind]
        original.touch()
        return {
            "id": asset_id,
            "session_id": "session",
            "mode": "Reference",
            "type": kind,
            "filename": original.name,
            "size": 0,
            "mime_type": f"{kind}/test",
            "reference": reference,
            "subject": None,
            "_original_path": str(original),
        }

    def store(self, root: Path, *assets) -> media.MediaStore:
        store = media.MediaStore()
        store.sessions["session"] = list(assets)
        store._save("session")
        return store

    def test_a_picture_can_be_bound_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(root, self.asset(root, "a1", "<Picture 1>"))
                assets = store.set_subject("session", ["a1"], 1)
                self.assertEqual(assets[0]["subject"], 1)
                assets = store.set_subject("session", ["a1"], None)
                self.assertIsNone(assets[0]["subject"])

    def test_a_whole_box_is_released_in_one_call(self):
        """Emptying a box is one gesture, so it must be one write.

        Nine requests would leave the box half-gone if the fourth failed.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(
                    root,
                    self.asset(root, "a1", "<Picture 1>"),
                    self.asset(root, "a2", "<Picture 2>"),
                )
                store.set_subject("session", ["a1", "a2"], 2)
                assets = store.set_subject("session", ["a1", "a2"], None)
                self.assertEqual([asset["subject"] for asset in assets], [None, None])

    def test_grouping_never_moves_a_reference_tag(self):
        """The reason this is not a reorder.

        <Picture 3> may already be typed into the brief. Dragging its card into
        a subject box says who she is; it does not repoint the tag.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(
                    root,
                    self.asset(root, "a1", "<Picture 1>"),
                    self.asset(root, "a2", "<Picture 2>"),
                    self.asset(root, "a3", "<Picture 3>"),
                )
                store.set_subject("session", ["a3"], 1)
                store.set_subject("session", ["a1"], 2)
                tags = {asset["id"]: asset["reference"] for asset in store.list("session")}
                self.assertEqual(tags, {"a1": "<Picture 1>", "a2": "<Picture 2>", "a3": "<Picture 3>"})

    def test_a_binding_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(root, self.asset(root, "a1", "<Picture 1>"))
                store.set_subject("session", ["a1"], 3)
                restarted = media.MediaStore()
                self.assertEqual(restarted.list("session")[0]["subject"], 3)

    def test_a_session_stored_before_subjects_reads_as_ungrouped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                old = self.asset(root, "a1", "<Picture 1>")
                del old["subject"]
                store = self.store(root, old)
                self.assertIsNone(store.manifest("session", "Reference")["assets"][0]["subject"])

    def test_only_a_picture_can_define_a_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(root, self.asset(root, "a1", "<Video 1>", kind="video"))
                with self.assertRaises(media.MediaError) as caught:
                    store.set_subject("session", ["a1"], 1)
                self.assertEqual(caught.exception.code, "INVALID_SUBJECT")

    def test_a_subject_outside_the_ceiling_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(root, self.asset(root, "a1", "<Picture 1>"))
                for value in (0, MAX_PEOPLE + 1):
                    with self.assertRaises(media.MediaError):
                        store.set_subject("session", ["a1"], value)

    def test_one_bad_id_moves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                store = self.store(root, self.asset(root, "a1", "<Picture 1>"))
                with self.assertRaises(media.MediaError):
                    store.set_subject("session", ["a1", "missing"], 1)
                self.assertIsNone(store.list("session")[0]["subject"])


if __name__ == "__main__":
    unittest.main()


class ColdStoreTests(unittest.TestCase):
    """Regrouping a session restored from disk.

    `get` reads whatever is already in memory, so without a load first the very
    first regrouping after a ComfyUI restart failed with MEDIA_NOT_FOUND --
    where `reorder`, which loads, succeeded on the same session.
    """

    def test_a_cold_store_can_still_regroup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(media, "CACHE_ROOT", root):
                warm = SubjectBindingTests().store(root, SubjectBindingTests.asset(root, "a1", "<Picture 1>"))
                warm._save("session")

                cold = media.MediaStore()
                assets = cold.set_subject("session", ["a1"], 1)
                self.assertEqual(assets[0]["subject"], 1)
