"""Tests for scripts/check_pr_claims.py. No network. Run: python3 -m unittest scripts/test_pr_claims.py -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pr_claims as c  # noqa: E402

BODY = """## Summary
Adds REQ-1 and REQ-2 to the export screen.

## Tested requirements
- REQ-1 export csv
- REQ-2 export pdf
- REQ-3 export audit

## Notes
Also touches REQ-9 (not a claim).
"""


class Claims(unittest.TestCase):
    def test_only_claim_sections_count(self):
        self.assertEqual(c.claimed_ids(BODY), ["REQ-1", "REQ-2", "REQ-3"])

    def test_untested_ids_are_found(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d, "tests")
            t.mkdir()
            (t / "export.test.ts").write_text("it('REQ-1 csv', ()=>{}); it('REQ-22 other', ()=>{})")
            Path(d, "src.ts").write_text("// REQ-2 mentioned in source only")
            self.assertEqual(c.untested(BODY, d), ["REQ-2", "REQ-3"])

    def test_prefix_ids_do_not_match(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d, "tests")
            t.mkdir()
            (t / "a.test.ts").write_text("REQ-12 REQ-1a")
            self.assertEqual(c.untested("## Tested\n- REQ-1\n", d), ["REQ-1"])


if __name__ == "__main__":
    unittest.main()
