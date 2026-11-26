"""Cross-check native continuations and source-specific conversions."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import rebuild, resume_search, run_search
from .checkpoint import History, candidate_row, dump
from .test_resume import score, worker


class BranchAndTreeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def fixture(self, source, nodes, direction="higher"):
        rows = []
        for step, (parent, value, group) in enumerate(nodes):
            path = f"s{step}.sources.json" if value is not None else None
            if path:
                dump(self.tmp/path, {"solve.py":f"x = {value}\n"})
            rows.append(candidate_row(step, parent, step, path, value, f"Trial {step}",
                                      error="failed" if value is None else None,
                                      provenance=dict(algorithm=source, branch=group)))
        p = self.tmp/"input.json"
        dump(p, dict(candidates=rows, source=source, task="task", task_description="Optimize x.",
                     metric="x", direction=direction, cutoff=len(rows)-1, task_system="System.",
                     evaluation_protocol={}))
        return p

    def test_native_splits_match_full_run(self):
        from collections import Counter
        for name in ("multi_ar", "aira_mcts"):
            for boundary in (1, 3, 5):
                with self.subTest(name=name, boundary=boundary):
                    directory = self.tmp/f"{name}-{boundary}"
                    full, split = [], []
                    def runner(calls):
                        def model(role, prompt):
                            calls.append((role, prompt))
                            return worker(role, prompt)
                        return model
                    cfg = dict(method=name, sources={"solve.py":"x = 0\n"}, task="Improve x.",
                               task_system="System.", baseline=0, branches=4, evaluate=score)
                    all_rows = run_search(**cfg, budget=7, output=directory/"full", model=runner(full))
                    prefix = run_search(**cfg, budget=boundary, output=directory/"part", model=runner(split))
                    tail = resume_search(directory/"part"/"checkpoint.json", method=name, history_mode="inherit",
                                         budget=7-boundary, output=directory/"end", evaluate=score, model=runner(split))
                    self.assertEqual(prefix+tail, all_rows)
                    # Parallel edits can arrive in a different order.
                    self.assertEqual(Counter(split), Counter(full))
                    self.assertEqual([c for c in split if "Propose one" in c[0]],
                                     [c for c in full if "Propose one" in c[0]])

    def test_shared_ar_background_survives_partial_wave_and_historical_cutoff(self):
        p = self.fixture("ar", [(None,0,None),(0,4,None),(1,8,None)])
        cfg = dict(method="multi_ar", history_mode="inherit", evaluate=score)
        all_prompts, tail_prompts = [], []
        def model(calls):
            def call(role, prompt):
                if "Propose one" in role:
                    calls.append(prompt)
                return worker(role, prompt)
            return call
        run = resume_search(p, **cfg, budget=7, branches=4, output=self.tmp/"full", model=model(all_prompts))
        cropped = json.loads((self.tmp/"full"/"checkpoint.json").read_text())
        cropped["cutoff"] = 3
        dump(self.tmp/"full"/"at3.json", cropped)
        tail = resume_search(self.tmp/"full"/"at3.json", **cfg, budget=6, output=self.tmp/"end",
                             model=model(tail_prompts))
        self.assertEqual(tail, run[1:])
        self.assertEqual(tail_prompts, all_prompts[1:])
        self.assertIn("Shared AR history", tail_prompts[0])

    def test_flatten_and_record_breaker_seeds(self):
        for src in ("multi_ar", "aira_mcts"):
            p = self.fixture(src, [(None,2,None),(0,9,0),(0,5,1),(2,None,1),(1,11,0)])
            captured = []
            def model(role, prompt):
                captured.append(prompt)
                return worker(role, prompt)
            row, = resume_search(p, method="ar", history_mode="inherit", budget=1,
                                 output=self.tmp/(src+"ar"), model=model, evaluate=score)
            self.assertEqual(row["parent"], 4)
            self.assertRegex(captured[0], r"2\s+\|\s+5\.000000\s+\|\s+discard")
            self.assertNotIn("Cross-branch", captured[0])
        rows = resume_search(p, method="multi_ar", history_mode="inherit", branches=3, budget=3,
                             output=self.tmp/"seeds", model=worker, evaluate=score)
        self.assertEqual([r["parent"] for r in rows], [4,1,0])

    def test_common_ancestor_counted_once(self):
        p = self.fixture("multi_ar", [(None,0,None),(0,4,0),(1,7,0),(1,None,1),(3,6,1)])
        h = History.read(p)
        root,j,_,_ = rebuild.mcts(h, True, h.sources(h.best))
        self.assertEqual([n.node_id for n in root.children], [1])
        self.assertEqual([n.node_id for n in root.children[0].children], [2,3])
        self.assertEqual((root.explore_count,root.cumulative_value), (3,17))
        self.assertEqual(j.nodes[-1].parent.node_id, 3)

    def test_branch_rank_and_reference_exclusion(self):
        for direction, expected in [("higher",[1,2]),("lower",[0,2])]:
            # The shared seed is a valid incumbent on every branch.
            vals = [5, 3, 9, 5, 4, 7, 7] if direction=="higher" else [5, 2, 9, 5, 4, 7, 3]
            p = self.fixture("multi_ar", list(zip([None,0,0,0,1,2,3], vals,
                                                  [None,0,1,2,0,1,2])), direction)
            h = History.read(p)
            db = rebuild.population(h, True, h.sources(h.best), islands=2)
            self.assertEqual([r["branch"] for r in db.branch_import[:2]], expected)
            ref = db.branch_import[-1]
            self.assertEqual(ref["role"], "reference")
            inactive = set(ref["steps"]) - {0}
            self.assertFalse(inactive & {p.step_id for p in db.programs.values()})
            self.assertIn(f"Branch {ref['branch']},", db.reference_summary)
            rows = resume_search(p, method="openevolve", history_mode="inherit", budget=4, islands=2,
                                 output=self.tmp/direction, model=worker, evaluate=score)
            self.assertFalse(any(r["parent"] in inactive for r in rows))
            self.assertIn("reference only", rows[0]["proposal"])
            self.assertEqual(json.loads((self.tmp/direction/"branch_import.json").read_text()),db.branch_import)

    def test_nested_forks_and_chain(self):
        p = self.fixture("aira_mcts", [(None,0,None),(0,1,None),(0,2,None),(1,3,None),
                                       (1,4,None),(2,5,None),(3,6,None),(4,7,None),(7,None,None)])
        h = History.read(p)
        def partition(n):
            db = rebuild.population(h, True, h.sources(h.best), islands=n)
            return [set(db.programs[i].step_id for i in s) for s in db.islands]
        two = partition(2)
        self.assertIn({1,3,4,6,7},two)
        self.assertIn({0,2,5},two)
        three = partition(3)
        self.assertIn({0,2,5},three)
        self.assertTrue(any({3,6}.issubset(s) for s in three))
        self.assertTrue(any({4,7}.issubset(s) for s in three))
        p = self.fixture("aira_mcts", [(None,0,None),(0,1,None),(1,2,None),(2,3,None)])
        h = History.read(p)
        self.assertEqual(partition(3), [{0,1,2,3},set(),set()])
