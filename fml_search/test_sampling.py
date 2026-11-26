import io
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from . import APIError, sample_search
from . import candidate
from .control import decide
from .test_search import edit
from .test_resume import score, worker


def accept(role, prompt):
    data = json.loads(prompt)
    return json.dumps(dict(verdict="ok", change_summary="Changed " + data["candidate_source"]["solve.py"].strip(),
                           feedback=""))


class SamplingTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)/"sample"
        self.args = dict(sources={"solve.py":"x = 0\n"}, baseline=0, task="Improve x.",
                         allowed_scope=["solve.py"], baseline_parameters={"x":0}, output=self.out,
                         evaluate=score, model=worker, review_model=accept)

    def test_independent_observations_and_repeats(self):
        seen = []
        choices = ["multi_ar","multi_ar","ar","multi_ar"]
        def controller(role, prompt):
            obs = json.loads(prompt)
            seen.append(obs)
            return json.dumps(dict(reason="Use the observed progress.", config_id=choices[obs["budget"]["decision_index"]]))
        result = sample_search("independent", **self.args, controller_model=controller, B=11,T=4,K=2,R=2)
        self.assertEqual(result["executed_candidate_slots"], 22)
        self.assertEqual(len(result["leaves"]), 2)
        for i, o in enumerate(seen):
            idx = i%4
            self.assertEqual(o["budget"]["used"], [0,2,5,8][idx])
            self.assertEqual(o["budget"]["next_stage_budget"], [2,3,3,3][idx])
            flat = [r for s in o["research_history"] for b in s["branches"].values() for r in b]
            self.assertEqual(sorted(r["current_id"] for r in flat), list(range(1, o["budget"]["used"]+1)))
            for r in flat:
                self.assertEqual(r["status"], "valid")
                self.assertNotEqual(r["change_summary"], r["proposal"])
                self.assertLess(r["parent_id"],r["current_id"])
            if idx == 2:
                self.assertEqual(o["current_state"]["consecutive_stages"], 2)
                self.assertEqual(o["current_state"]["consecutive_candidates"], 5)
                self.assertEqual([b["best_score"] for b in o["current_state"]["branches"]], [3,2])
                self.assertEqual([r["round_idx"] for r in o["research_history"][1]["branches"]["0"]], [0,1])
        self.assertEqual(result["leaves"][0]["configs"],choices)
        leaf = json.loads((self.out/result["leaves"][0]["path"]).read_text())
        self.assertEqual(leaf["budget"]["next_stage_budget"], 0)
        self.assertEqual(sum(len(b) for s in leaf["research_history"] for b in s["branches"].values()),11)

    def test_full_tree_executes_each_edge_once(self):
        calls = []
        lock = threading.Lock()
        def evaluate(path):
            with lock:
                calls.append(path)
            return score(path)
        self.args["evaluate"] = evaluate
        result = sample_search("tree", **self.args, B=4,T=4,K=2,R=2,
                               controller_model=lambda *_: self.fail("Controller called for enumeration"))
        self.assertEqual(result["completed_edges"],60)
        self.assertEqual(result["executed_candidate_slots"],60)
        self.assertEqual(len(result["leaves"]),32)
        self.assertEqual(len(set(tuple(l["configs"]) for l in result["leaves"])),16)
        self.assertEqual(len(set(calls)),60)
        self.assertEqual(len(list(self.out.rglob("trajectory.json"))),60)

    def test_review_and_execution_share_repair_allowance(self):
        edits = iter([1,2,3,0,0,0])
        review_inputs = []
        prompts = []
        def edit_model(role,prompt):
            if "editing" in role:
                prompts.append(prompt)
                return edit(next(edits))
            return "Proposed change."
        def review(role,prompt):
            review_inputs.append(json.loads(prompt))
            if len(review_inputs) == 1:
                return json.dumps(dict(verdict="no_op",change_summary="Ineffective path.",feedback="Edit the active path."))
            return accept(role,prompt)
        evaluations = iter([float("nan"),3.0,0.0])
        self.args.update(model=edit_model,review_model=review,evaluate=lambda _:next(evaluations))
        choose_ar = lambda *_: '{"reason":"Use AR.","config_id":"ar"}'
        sample_search("independent", **self.args, B=1,T=1,controller_model=choose_ar)
        checkpoint = next(self.out.rglob("trajectory.json")).parent / "checkpoint.json"
        row = json.loads(checkpoint.read_text())["candidates"][-1]
        self.assertEqual(row["provenance"]["candidate_status"], "valid")
        self.assertEqual(row["provenance"]["change_summary"], "Changed x = 3")
        self.assertEqual(len(prompts),3)
        self.assertIn("Edit the active path.",prompts[1])
        self.assertIn("non_finite_metric",prompts[2])
        self.assertTrue(all(r["parent_source"]==self.args["sources"] for r in review_inputs))

    def test_noop_even_when_reviewer_accepts_identical_code(self):
        self.args.update(model=lambda role,prompt:edit(0) if "editing" in role else "Try changing x.",
                         evaluate=lambda _:self.fail("No-op evaluated"))
        sample_search("independent", **self.args,B=1,T=1,max_repairs=1,
                      controller_model=lambda *_:'{"reason":"Use AR.","config_id":"ar"}')
        t=json.loads(next(self.out.rglob("trajectory.json")).read_text())["records"][0]
        self.assertEqual(t["candidate_status"],"no-op")
        self.assertEqual(len(t["attempts"]),2)
        self.assertTrue(all("review" in a for a in t["attempts"]))

    def test_strict_json_control_and_wire_format(self):
        responses=iter(["", '{"config_id":"openevolve","reason":"Try it"}'])
        with self.assertRaises(APIError):
            decide(lambda *_:next(responses),{})
        def response(req,timeout):
            payload=json.loads(req.data)
            self.assertEqual(payload["response_format"],{"type":"json_object"})
            self.assertEqual(payload["thinking"],{"type":"disabled"})
            return io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
        with patch.dict(os.environ, DEEPSEEK_API_KEY="offline-test"), patch.object(
                candidate, "urlopen", side_effect=response):
            self.assertEqual(candidate.deepseek_json("JSON","test"),"{}")
