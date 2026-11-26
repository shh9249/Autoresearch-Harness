"""Run from the workspace root with unittest; no network or GPU is required."""
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock

from . import APIError, run_search
from .editor import EditError, apply_response, replace_block


def edit(value):
    fence = chr(96) * 3
    return f"### FILE: solve.py\n{fence}python\nx = {value}\n{fence}"


class FreshRunTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sources = {"solve.py": "x = 0\n"}

    def run_case(self, method, model, evaluate, budget=1, **options):
        return run_search(method, sources=self.sources, baseline=0, task="Increase x.",
                          budget=budget, output=self.root/method, model=model, evaluate=evaluate,
                          **options)

    def test_four_algorithms(self):
        def model(role, prompt):
            if "editing" in role:
                old = int(re.search(r"### FILE: solve.py\nx = (\d+)", prompt)[1])
                return edit(old + 1)
            return "Increase x by one."
        def score(directory):
            return int((directory/"solve.py").read_text().split()[-1])
        for name in ("ar","multi_ar","aira_mcts","openevolve"):
            with self.subTest(name=name):
                records = self.run_case(name, model, score, budget=9)
                self.assertEqual(len(records), 9)
                self.assertTrue(all(r["status"] == "success" for r in records))
                self.assertTrue(any(r["parent"] != 0 for r in records))
                self.assertEqual(json.loads((self.root/name/"trajectory.json").read_text())["records"], records)

    def test_failures_do_not_cross_slots(self):
        proposals = iter(["", "Original research intent.", "Fresh independent proposal."])
        responses = iter(["invalid", edit(1), edit(2), edit(2), edit(2), edit(2)])
        feedback = []
        scores = iter([float("nan"), 2])
        def model(role, prompt):
            if "editing" not in role:
                return next(proposals)
            feedback.append(prompt)
            return next(responses)
        rows = self.run_case("ar", model, lambda _: next(scores), budget=3)
        self.assertEqual([r["status"] for r in rows], ["proposal_failed","success","candidate_failed"])
        self.assertEqual([len(r["attempts"]) for r in rows], [0,3,3])
        self.assertIn("malformed_response", feedback[1])
        self.assertNotIn("Original research intent.", feedback[1])
        self.assertIn("non_finite_metric", feedback[2])
        self.assertIn("Fresh independent proposal.", feedback[3])
        self.assertEqual(rows[2]["parent"], 2)

    def test_branch_barriers_and_shared_journal(self):
        proposals, gate, lock = [], Barrier(2), Lock()
        def model(role, prompt):
            if "editing" in role:
                gate.wait(timeout=5)
                return edit(1)
            with lock:
                proposals.append(prompt)
                return f"Unique plan {len(proposals)}"
        def score(_):
            gate.wait(timeout=5)
            return 1
        self.run_case("multi_ar", model, score, budget=4, branches=2)
        self.assertIn("Unique plan 1", proposals[1])
        self.assertIn("Unique plan 2", proposals[2])
        self.assertIn("Unique plan 1", proposals[3])

    def test_exhausted_proposal_and_infrastructure_failure(self):
        def unavailable(role, prompt):
            raise APIError("Test provider failure.")
        rows = self.run_case("ar", unavailable, lambda _: self.fail("Unexpected evaluation"))
        self.assertEqual(rows[0]["status"], "proposal_failed")
        self.assertEqual(rows[0]["attempts"], [])
        def unavailable_editor(role, prompt):
            if "editing" in role:
                raise APIError("Test editor transport failure.")
            return "Change x."
        with self.assertRaises(APIError):
            self.run_case("multi_ar", unavailable_editor, lambda _: self.fail("Unexpected evaluation"))
        record = json.loads((self.root/"multi_ar"/"slot-00001"/"result.json").read_text())
        self.assertEqual(record["status"], "infrastructure_error")
        self.assertEqual(len(record["attempts"]), 1)

    def test_rejected_edit_publishes_no_attempt_directory(self):
        def model(role, prompt):
            if "editing" in role:
                return edit(1) + "\n### FILE: missing.py"
            return "Change x."
        row, = self.run_case("ar", model, lambda _: self.fail("Unexpected evaluation"), max_repairs=0)
        self.assertEqual(row["status"], "candidate_failed")
        self.assertFalse((self.root/"ar"/"slot-00001"/"attempt-0").exists())

    def test_editor(self):
        self.assertEqual(replace_block("  a\n  b", "a\nb", "c\n  d"), "  c\n    d")
        self.assertEqual(replace_block("  a\na", "a", "b"), "  a\nb")
        with self.assertRaises(EditError) as ambiguous:
            replace_block("  a\n    a", "a", "b")
        self.assertEqual(ambiguous.exception.feedback["code"], "ambiguous_search")
        with self.assertRaises(EditError) as failed:
            apply_response(edit(1) + "\n<<<<<<< SEARCH\nx = 20\n=======\nx = 3\n>>>>>>> REPLACE", self.sources)
        self.assertIn("nearby_source", failed.exception.feedback)
        self.assertEqual(self.sources["solve.py"], "x = 0\n")
