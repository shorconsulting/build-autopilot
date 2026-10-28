import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "read_cap.py")


class ReadCap(unittest.TestCase):
    def run_hook(self, cap, lines=400, **inp):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".github"))
            if cap is not None:
                with open(os.path.join(tmp, ".github", "autopilot.json"), "w") as fh:
                    json.dump({"read_cap": cap}, fh)
            big = os.path.join(tmp, "big.txt")
            with open(big, "w") as fh:
                fh.write("x\n" * lines)
            stdin = json.dumps({"tool_name": "Read", "cwd": tmp, "tool_input": dict(file_path=big, **inp)})
            out = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True).stdout
            return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "allow"

    def test_big_file_without_limit_is_denied(self):
        self.assertEqual(self.run_hook(True), "deny")

    def test_with_limit_is_allowed(self):
        self.assertEqual(self.run_hook(True, limit=50), "allow")

    def test_flag_off_or_missing_is_allowed(self):
        self.assertEqual(self.run_hook(False), "allow")
        self.assertEqual(self.run_hook(None), "allow")

    def test_small_file_is_allowed(self):
        self.assertEqual(self.run_hook(True, lines=300), "allow")


if __name__ == "__main__":
    unittest.main()
