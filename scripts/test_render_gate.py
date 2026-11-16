"""Tests for scripts/render_gate.py: the gate's workflow on the repo's chosen runner.

No network. Run:
    python3 -m unittest scripts/test_render_gate.py -v
"""
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_gate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "templates" / "github" / "workflows" / "autopilot.yml").read_text(encoding="utf-8")
SELF = ["self-hosted", "shorconsulting-ci"]


def runs_on(text):
    return [m.group("val") for m in render_gate.RUNS_ON.finditer(text)]


class Choose(unittest.TestCase):
    def test_no_runner_keeps_the_template(self):
        self.assertEqual(render_gate.choose({}), ["ubuntu-24.04"])
        self.assertEqual(render_gate.render(TEMPLATE, ["ubuntu-24.04"]), TEMPLATE)

    def test_policy_list_or_string(self):
        self.assertEqual(render_gate.choose({"runner": SELF}), SELF)
        self.assertEqual(render_gate.choose({"runner": "self-hosted, shorconsulting-ci"}), SELF)
        self.assertEqual(render_gate.choose({"runner": "my-box"}), ["my-box"])
        self.assertEqual(render_gate.choose({"runner": ""}), ["ubuntu-24.04"])

    def test_bad_label_is_refused(self):
        with self.assertRaises(ValueError):
            render_gate.choose({"runner": "ubuntu; rm -rf /"})

    def test_existing_workflow_is_kept_when_the_policy_says_nothing(self):
        current = render_gate.render(TEMPLATE, SELF)
        self.assertEqual(render_gate.choose({}, current), SELF)

    def test_policy_wins_over_existing(self):
        current = render_gate.render(TEMPLATE, SELF)
        self.assertEqual(render_gate.choose({"runner": "ubuntu-24.04"}, current), ["ubuntu-24.04"])

    def test_mixed_existing_with_the_template_runner_falls_back(self):
        mixed = TEMPLATE.replace("runs-on: ubuntu-24.04", "runs-on: [self-hosted, x]", 1)
        self.assertEqual(render_gate.choose({}, mixed), ["ubuntu-24.04"])

    def test_mixed_existing_without_the_template_runner_stops(self):
        current = render_gate.render(TEMPLATE, SELF).replace("runs-on: [self-hosted, shorconsulting-ci]",
                                                              "runs-on: my-box", 1)
        with self.assertRaises(ValueError):
            render_gate.choose({}, current)
        self.assertEqual(render_gate.choose({"runner": "my-box"}, current), ["my-box"])

    def test_quoted_existing_label_is_read(self):
        current = TEMPLATE.replace("runs-on: ubuntu-24.04", 'runs-on: "my-box"')
        self.assertEqual(render_gate.choose({}, current), ["my-box"])

    def test_unreadable_existing_says_what_to_do(self):
        current = TEMPLATE.replace("runs-on: ubuntu-24.04", "runs-on: ${{ matrix.os }}")
        with self.assertRaisesRegex(ValueError, "set `runner`"):
            render_gate.choose({}, current)


class Render(unittest.TestCase):
    def test_every_job_moves_and_nothing_else_changes(self):
        out = render_gate.render(TEMPLATE, SELF)
        self.assertEqual(len(runs_on(TEMPLATE)), 4)
        self.assertEqual(runs_on(out), ["[self-hosted, shorconsulting-ci]"] * 4)
        strip = lambda t: re.sub(r"runs-on:[^\n]*", "runs-on:", t)
        self.assertEqual(strip(out), strip(TEMPLATE))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            pol = Path(d) / "autopilot.json"
            pol.write_text(json.dumps({"runner": SELF}))
            buf = io.StringIO()
            with redirect_stdout(buf):
                render_gate.main(["--which", str(pol)])
            self.assertEqual(json.loads(buf.getvalue()), SELF)
            tpl = Path(d) / "t.yml"
            tpl.write_text(TEMPLATE)
            buf = io.StringIO()
            with redirect_stdout(buf):
                render_gate.main([str(tpl), str(pol), "--existing", str(Path(d) / "missing.yml")])
            self.assertIn("runs-on: [self-hosted, shorconsulting-ci]", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
