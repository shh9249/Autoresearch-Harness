"""Regression tests for prefix isolation, target reconstruction and identity continuity."""
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import resume_search, run_search
from . import rebuild
from .checkpoint import History, candidate_row, dump
from .test_search import edit


METHODS = ("ar", "multi_ar", "aira_mcts", "openevolve")


def worker(role, prompt):
    if "editing existing source" in role:
        value = int(re.search(r"### FILE: solve.py\nx = (\d+)", prompt)[1])
        return edit(value + 1)
    return "Increase the parameter by one."


def score(directory):
    return int((directory / "solve.py").read_text().split()[-1])


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        rows = []
        for cid, parent, step, value, desc in [
                ("base", None, 0, 1, "Original baseline"),
                ("best", "base", 10, 7, "HISTORY_MARKER"),
                ("error", "best", 19, None, "Broken edit"),
                ("other", "best", 20, 4, "Another implementation"),
                ("future", "other", 21, 500, "FUTURE_MARKER")]:
            path = f"{step}.sources.json" if value is not None else None
            if value is not None and step <= 20:
                dump(self.root / path, {"solve.py": f"x = {value}\n"})
            rows.append(candidate_row(cid, parent, step, path, value, desc,
                                      error="edit failed" if value is None else None))
        self.path = self.root / "input.json"
        self.data = dict(task="example", task_system="TASK_SYSTEM_MARKER", metric="score",
                         direction="higher", candidates=rows, cutoff=20, source="external",
                         task_description="Maximize x.", evaluation_protocol={"seed": 0})
        dump(self.path, self.data)

    def test_cutoff_and_both_modes_on_every_target(self):
        before = self.path.read_bytes()
        for method in METHODS:
            for mode in ("best_only", "inherit"):
                with self.subTest(method=method, mode=mode):
                    seen = []
                    def model(role, prompt):
                        self.assertIn("TASK_SYSTEM_MARKER", role)
                        seen.append(prompt)
                        return worker(role, prompt)
                    out = self.root / (method + mode)
                    records = resume_search(self.path, method=method, history_mode=mode,
                                            budget=3, output=out, evaluate=score, model=model)
                    self.assertEqual([r["slot"] for r in records], [21,22,23])
                    self.assertTrue(all(r["status"] == "success" for r in records))
                    prefix = History.read(out / "checkpoint.json")
                    self.assertNotIn("future", prefix.by_id)
                    self.assertNotIn("FUTURE_MARKER", "\n".join(seen))
                    self.assertEqual(prefix.cutoff, 23)
                    self.assertEqual(prefix.meta["evaluation_protocol"], {"seed": 0})
                    for rec in records:
                        self.assertEqual(rec["parent_id"], prefix.by_step[rec["parent"]]["id"])
                    if mode == "best_only":
                        self.assertNotIn("HISTORY_MARKER", "\n".join(seen))
                        self.assertEqual(records[0]["parent_id"], "best")
                        self.assertEqual(records[0]["metric"], 8)
                    else:
                        self.assertIn("HISTORY_MARKER", "\n".join(seen))
        self.assertEqual(before, self.path.read_bytes())

    def test_state_initializers(self):
        hist = History.read(self.path)
        code = hist.sources(hist.best)
        heads, journal = rebuild.ar_heads(hist, "ar", 1, True, code)
        self.assertEqual([e.status for e in journal], ["keep", "crash", "discard"])
        self.assertEqual((heads[0].head, heads[0].best_metric), (10, 7))
        root, tree_journal, low, high = rebuild.mcts(hist, True, code)
        self.assertEqual((root.explore_count, root.cumulative_value), (2, 11))
        self.assertEqual(root.children[0].children[0].node_id, "other")
        self.assertEqual((low, high, len(tree_journal.buggy_nodes)), (4, 7, 1))
        db = rebuild.population(hist, True, code)
        self.assertEqual(db.programs[db.best_program_id].step_id, 10)
        self.assertTrue(db.archive and db._reference_programs)
        self.assertNotIn(19, {p.step_id for p in db.programs.values()})

    def test_best_only_loads_only_best_code_and_handles_id_collision(self):
        (self.root / "0.sources.json").unlink()
        (self.root / "20.sources.json").unlink()
        self.data["candidates"][0]["id"] = 21
        self.data["candidates"][1]["parent_id"] = 21
        dump(self.path, self.data)
        row, = resume_search(self.path, method="ar", history_mode="best_only", budget=1,
                             output=self.root/"collision", model=worker, evaluate=score)
        self.assertEqual((row["slot"], row["id"], row["parent_id"]), (21, "step-21", "best"))

    def test_chained_switches_keep_archive_and_global_steps(self):
        out = self.root / "fresh"
        run_search("ar", sources={"solve.py":"x = 0\n"}, baseline=0, task="Increase x.",
                   budget=2, output=out, model=worker, evaluate=score)
        for i, (method, mode) in enumerate(zip(METHODS, ("inherit","best_only","inherit","inherit"))):
            dest = self.root / method
            row, = resume_search(out/"checkpoint.json", method=method, history_mode=mode,
                                 budget=1, output=dest, model=worker, evaluate=score)
            self.assertEqual((row["slot"], row["id"]), (3+i, 3+i))
            out = dest
        hist = History.read(out/"checkpoint.json")
        self.assertEqual(len(hist.rows), 7)
        for row in hist.rows:
            self.assertEqual(hist.sources(row)["solve.py"], f"x = {row['score']}\n")

    def test_lower_metric_and_output_isolation(self):
        self.data["direction"] = "lower"
        dump(self.path, self.data)
        out = self.root / "isolated"
        row, = resume_search(self.path, method="ar", history_mode="best_only", budget=1,
                             output=out, model=worker, evaluate=score)
        self.assertEqual(row["parent_id"], "base")
        self.assertEqual(row["selection"], "discard")
        with self.assertRaises(FileExistsError):
            resume_search(self.path, method="ar", history_mode="inherit", budget=1,
                          output=out, model=worker, evaluate=score)
