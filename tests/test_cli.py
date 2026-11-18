import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from nlp_classroom_translation_memory.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state = str(Path(self._directory.name) / "state.json")

    def run_cli(self, *args):
        buffer = StringIO()
        with redirect_stdout(buffer):
            main(["--state", self.state, *args])
        return json.loads(buffer.getvalue())

    def test_cli_material_term_translation_and_lookup(self):
        material = self.run_cli("material-create", "--subject", "化学", "--title", "课件")
        segment = self.run_cli(
            "segment-add",
            "--material",
            material["material_id"],
            "--text",
            "催化剂降低活化能。",
            "--context",
            "课堂",
        )
        self.run_cli("term-confirm", "--term", "催化剂", "--subject", "化学", "--translation", "catalyst")
        self.run_cli(
            "translation-record", "--segment", segment["segment_id"], "--text", "Catalyst lowers activation energy."
        )
        matches = self.run_cli(
            "lookup", "--text", "催化剂降低活化能。", "--subject", "化学", "--context", "课堂"
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["subject"], "化学")
        # 不同学科不得复用。
        self.assertEqual(
            self.run_cli("lookup", "--text", "催化剂降低活化能。", "--subject", "生物"), []
        )

    def test_cli_revision_creates_review_that_can_be_resolved(self):
        material = self.run_cli("material-create", "--subject", "语文")
        segment = self.run_cli("segment-add", "--material", material["material_id"], "--text", "光合作用需要光能。")
        self.run_cli("term-confirm", "--term", "光合作用", "--subject", "语文", "--translation", "photosynthesis")
        self.run_cli("translation-record", "--segment", segment["segment_id"], "--text", "Light is needed.")
        self.run_cli(
            "term-confirm", "--term", "光合作用", "--subject", "语文", "--translation", "photosynthesis process"
        )
        reviews = self.run_cli("reviews", "--material", material["material_id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["expected_translation"], "photosynthesis process")
        self.run_cli(
            "review-resolve",
            "--review",
            reviews[0]["review_id"],
            "--text",
            "Photosynthesis process needs light.",
        )
        matches = self.run_cli("lookup", "--text", "光合作用需要光能。", "--subject", "语文")
        self.assertEqual(matches[0]["target_text"], "Photosynthesis process needs light.")

    def test_cli_legacy_records_coexist_with_memory_document(self):
        self.run_cli("material-create", "--subject", "生物")
        fixture = Path("fixtures/records.json")
        self.run_cli("ingest", str(fixture))
        listed = self.run_cli("list", "--source", "school-synthetic")
        self.assertEqual(len(listed), 2)
        document = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(len(document["materials"]), 1)
        self.assertEqual(len(document["records"]), 2)


if __name__ == "__main__":
    unittest.main()
