"""OE-source tests for migration aliases and exact native continuation."""
import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import rebuild, resume_search, run_search
from .checkpoint import History, candidate_row, dump
from .test_resume import METHODS, score, worker


class OESourceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def test_all_native_source_target_pairs(self):
        for source in METHODS:
            out = self.tmp/source
            run_search(source, sources={"solve.py":"x = 0\n"}, baseline=0, task="Increase x.",
                       budget=3, output=out, model=worker, evaluate=score)
            for target in METHODS:
                with self.subTest(source=source, target=target):
                    dest = self.tmp/f"{source}-to-{target}"
                    rows = resume_search(out/"checkpoint.json", method=target, history_mode="inherit",
                                         budget=2, output=dest, model=worker, evaluate=score)
                    self.assertEqual([r["slot"] for r in rows], [4,5])
                    self.assertTrue(all(r["status"]=="success" for r in rows))
                    hist = History.read(dest/"checkpoint.json")
                    for r in rows:
                        self.assertEqual(hist.by_id[r["parent_id"]]["step"], r["parent"])

    def test_frozen_wave_rng_and_migration_resume(self):
        original, restored = [], []
        def capture(calls):
            def model(role, prompt):
                calls.append((role, prompt))
                return worker(role, prompt)
            return model
        out = self.tmp/"full"
        all_rows = run_search("openevolve", sources={"solve.py":"x = 0\n"}, baseline=0,
                              task="Increase x.", budget=24, islands=2, output=out,
                              evaluate=score, model=capture(original))
        data = json.loads((out/"checkpoint.json").read_text())
        for cut in (1,19,20):
            with self.subTest(cut=cut):
                restored.clear()
                data["cutoff"] = cut
                dump(out/"prefix.json", data)
                dest = self.tmp/f"resumed-{cut}"
                tail = resume_search(out/"prefix.json", method="openevolve", history_mode="inherit",
                                     budget=24-cut, output=dest, evaluate=score, model=capture(restored))
                self.assertEqual(restored, original[2*cut:])
                self.assertEqual(tail, all_rows[cut:])
                def native(path):
                    with gzip.open(path, "rt", encoding="utf-8") as stream:
                        return json.load(stream)
                ref = native(out/data["oe_states"]["24"])
                self.assertTrue(any(p["migrated"] for p in ref["programs"]))
                self.assertEqual(ref, native(dest/"oe-state"/"00000024.json.gz"))
                h = History.read(dest/"checkpoint.json")
                tree, journal, _, _ = rebuild.mcts(h, True, h.sources(h.best))
                self.assertEqual(len(journal.nodes), 24)
                self.assertEqual(tree.explore_count, 24)

    def test_cross_targets_resolve_migration_copies(self):
        rows = []
        items = [(0,None,0,1,"seed"),(1,0,1,7,"evolve"),("m1",1,1,7,"migration"),
                 ("m2","m1",1,7,"migrate"),(2,"m2",2,5,"evolve"),(3,2,3,None,"evolve"),
                 (4,2,4,None,"evolve"),(5,2,5,9,"evolve")]
        for cid, parent, step, value, action in items:
            path = f"{step}.sources.json" if value is not None else None
            if path:
                dump(self.tmp/path, {"solve.py":f"x = {value}\n"})
            rows.append(candidate_row(cid, parent, step, path, value, f"Trial {cid}",
                                      provenance=dict(algorithm="openevolve", native_action=action,
                                                      evaluated=cid != 4)))
        p = self.tmp/"external.json"
        dump(p, dict(source="openevolve",candidates=rows,cutoff=5,task="x",metric="x",direction="higher",
                     task_description="Improve x.",evaluation_protocol={},task_system=""))
        h = History.read(p)
        self.assertEqual(h.by_id[2]["parent_id"],1)
        tree, journal, _, _ = rebuild.mcts(h,True,h.sources(h.best))
        self.assertEqual([n.node_id for n in journal.nodes], [1,2,3,5])
        self.assertEqual((tree.explore_count,tree.cumulative_value),(3,21))
        for method in ("ar","multi_ar","aira_mcts"):
            calls = []
            def model(role, prompt):
                calls.append(prompt)
                return worker(role, prompt)
            records = resume_search(p, method=method, history_mode="inherit", budget=3,
                                    output=self.tmp/method, model=model, evaluate=score)
            self.assertNotIn("Trial m1", calls[0])
            self.assertNotIn("Trial 4", calls[0])
            if method == "ar":
                self.assertEqual(records[0]["parent_id"],5)
                self.assertIn("Trial 3",calls[0])
            elif method == "multi_ar":
                self.assertEqual([r["parent_id"] for r in records],[5,1,0])
        with self.assertRaisesRegex(ValueError, "native state sidecar"):
            resume_search(p, method="openevolve", history_mode="inherit", budget=1,
                          output=self.tmp/"missing", model=worker, evaluate=score)
