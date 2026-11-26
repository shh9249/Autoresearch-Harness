"""AR-source conversion tests at real public checkpoint boundaries."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import rebuild, resume_search, run_search, upstream
from .checkpoint import History, candidate_row, dump
from .test_resume import score, worker
from .test_search import edit


class ARSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def history(self, entries, direction="higher"):
        rows = []
        for step, (parent, value) in enumerate(entries):
            path = None if value is None else f"{step}.sources.json"
            if path:
                dump(self.dir / path, {"solve.py": f"x = {value}\n"})
            rows.append(candidate_row(step, parent, step, path, value, f"Intent {step}",
                                      error="Evaluation failed." if value is None else None))
        path = self.dir / "input.json"
        dump(path, dict(source="ar", task="example", metric="score", direction=direction,
                        candidates=rows, cutoff=len(rows)-1, task_description="Improve x.",
                        task_system="System marker.", evaluation_protocol={}))
        return History.read(path)

    def test_ar_split_and_uninterrupted_calls_match(self):
        def scripted(calls, plans, edits):
            plans, edits = iter(plans), iter(edits)
            def model(role, prompt):
                calls.append((role, prompt))
                return edit(next(edits)) if "editing existing" in role else next(plans)
            return model
        cfg = dict(method="ar", sources={"solve.py":"x = 0\n"}, baseline=0,
                   task="Increase x.", task_system="Research.", evaluate=score, max_repairs=0)
        plans = ["Improve", "Non-improvement", "", "Change", "Next"]
        edits = [5, 3, 5, 6]
        entire, split = [], []
        expected = run_search(**cfg, budget=5, output=self.dir/"full", model=scripted(entire, plans, edits))
        run_search(**cfg, budget=4, output=self.dir/"prefix", model=scripted(split, plans[:4], edits[:3]))
        observed, = resume_search(self.dir/"prefix"/"checkpoint.json", method="ar", history_mode="inherit",
                                  budget=1, output=self.dir/"end", evaluate=score,
                                  model=scripted(split, plans[-1:], edits[-1:]), max_repairs=0)
        self.assertEqual(entire, split)
        self.assertEqual(expected[-1], observed)

    def test_native_ar_after_best_only(self):
        self.history([(None, 0), (0, 3), (1, 2)])
        seen = []
        def track(role, prompt):
            seen.append((role, prompt))
            return worker(role, prompt)
        def resumed(path, name, mode, budget):
            return resume_search(path, method="ar", history_mode=mode, budget=budget,
                                 output=self.dir/name, model=track, evaluate=score)
        resumed(self.dir/"input.json", "full", "best_only", 2)
        expected = list(seen)
        seen.clear()
        resumed(self.dir/"input.json", "prefix", "best_only", 1)
        resumed(self.dir/"prefix"/"checkpoint.json", "end", "inherit", 1)
        self.assertEqual(seen, expected)

    def test_branch_record_breakers_and_common_history(self):
        for direction, scores in [("higher", [2,5,4,5,9]), ("lower", [9,5,7,5,2])]:
            with self.subTest(direction=direction):
                self.history(list(zip([None,0,1,1,1], scores)), direction)
                proposals = []
                def model(role, prompt):
                    if "editing existing" in role:
                        return worker(role, prompt)
                    proposals.append(prompt)
                    return f"Sibling plan {len(proposals)}"
                rows = resume_search(self.dir/"input.json", method="multi_ar", history_mode="inherit",
                                     branches=3, budget=4, output=self.dir/direction, model=model, evaluate=score)
                self.assertEqual([r["parent"] for r in rows[:3]], [4,1,0])
                for prompt in proposals:
                    self.assertIn("Shared AR history before branching", prompt)
                    self.assertIn("Intent 2", prompt)
                self.assertIn("Sibling plan 1", proposals[1])
                self.assertIn("Sibling plan 2", proposals[2])
                self.assertIn("Sibling plan 3", proposals[3])
                self.assertIn("no experiments on this branch yet", proposals[0])

    def test_mcts_retains_failed_edges_and_uses_tree(self):
        hist = self.history([(None,1), (0,7), (1,None), (2,4), (3,None)])
        root, journal, low, high = rebuild.mcts(hist, True, hist.sources(hist.best))
        nodes = {n.step_id:n for n in journal.nodes}
        self.assertIs(nodes[3].parent, nodes[2])
        self.assertEqual(nodes[3].children, [nodes[4]])
        self.assertEqual((root.explore_count, root.cumulative_value), (2,11))
        self.assertEqual((nodes[2].explore_count, nodes[2].cumulative_value), (1,4))
        self.assertEqual(nodes[4].explore_count, 0)
        self.assertEqual([n.step_id for n in upstream.search_policy(root,1.41,high,low,"higher")], [0,1,2,3])
        row, = resume_search(self.dir/"input.json", method="aira_mcts", history_mode="inherit",
                             budget=1, output=self.dir/"mcts", model=worker, evaluate=score)
        self.assertEqual(row["parent_id"], 3)

    def test_oe_uses_lineages_only_and_can_run_past_migration(self):
        hist = self.history([(None,1),(0,3),(1,9),(1,2),(2,11),(3,4),(1,None)])
        db = rebuild.population(hist, True, hist.sources(hist.best))
        by_step = {p.step_id:p for p in db.programs.values()}
        self.assertEqual(set(by_step), {0,1,2,3,4,5})
        self.assertEqual(by_step[1].island, by_step[2].island)
        self.assertEqual(by_step[4].island, by_step[2].island)
        self.assertEqual(by_step[3].island, by_step[5].island)
        self.assertNotEqual(by_step[2].island, by_step[3].island)
        self.assertEqual(by_step[3].parent_id, by_step[1].id)
        self.assertEqual(len([s for s in db.islands if s]), 2)
        hist = self.history([(None,1),(0,3),(1,4),(2,None)])
        db = rebuild.population(hist, True, hist.sources(hist.best))
        self.assertEqual([len(s) for s in db.islands], [3,0,0])
        rows = resume_search(self.dir/"input.json", method="openevolve", history_mode="inherit",
                             budget=14, output=self.dir/"oe", model=worker, evaluate=score)
        self.assertTrue(all(r["status"] == "success" for r in rows))
        self.assertTrue(all(r["group"] == 0 for r in rows[:10]))
        self.assertTrue(all(r["parent_id"] is not None for r in rows))
