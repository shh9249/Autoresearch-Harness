"""AHC026 live smoke entry point. Invoke one part at a time from the workspace root."""
import argparse
import json
import os
from functools import partial
from pathlib import Path
from threading import BoundedSemaphore

from ..benchmarks.ale import adapter
from .api_relay import request
from .candidate import CandidateFailure, deepseek, deepseek_json
from .checkpoint import dump
from .control import review_edit
from .sampling import sample_search
from .search import resume_search, run_search


def run_part(part, root, relay=None):
    if relay is None and not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY must be available in the launcher process.")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = adapter.workspace_root()
    sample = Path(adapter.__file__).parent/"samples"/"valid.py"
    task = (workspace/"bench_setup_2026-11-25/ale/data/ahc026/ahc026/statement_en.md").read_text(encoding="utf-8")
    sources = {"solve.py":sample.read_text(encoding="utf-8")}
    gate = BoundedSemaphore(4)
    def plain(role, prompt):
        with gate:
            if relay is not None:
                return request(relay, role, prompt, False)
            return deepseek(role, prompt)
    def structured(role, prompt):
        with gate:
            if relay is not None:
                return request(relay, role, prompt, True)
            return deepseek_json(role, prompt)
    def evaluate(directory):
        result = adapter.evaluate(dict(task_id="ahc026", public_seed=0, candidate_id=str(directory),
                                       source_path=str(directory/"solve.py"), language="python",
                                       config={"run_root":str(root/"judge")}))
        dump(directory/"judge.json", {k:result[k] for k in ("status","score","result_path","elapsed_seconds")})
        if result["status"] == "success":
            return result["score"]
        error = result["error"]
        if result["status"] in ("compile_error","runtime_error","timeout","invalid_output","output_limit_exceeded"):
            detail = error["message"]
            stderr = result["phases"].get(result["stage"],{}).get("stderr_path")
            if stderr and Path(stderr).is_file():
                detail += "\n" + Path(stderr).read_text(encoding="utf-8", errors="replace")[-1500:]
            raise CandidateFailure(error["code"], detail)
        raise RuntimeError(f"Judge error: {error}; see {result['result_path']}")
    target = root/part
    if target.exists():
        raise FileExistsError(f"Output exists: {target}")
    tree = root/"tree"/"rep-000"
    if part == "switches":
        for name in ("ar","multi_ar"):
            if not (tree/f"s0-{name}"/"run"/"trajectory.json").is_file():
                raise ValueError("Complete the tree part in the same root before running switches.")
    # Always measure the supplied solver with the exact public instance and protocol.
    baseline_dir = root/("baseline-"+part)
    baseline_dir.mkdir(exist_ok=True)
    (baseline_dir/"solve.py").write_text(sources["solve.py"], encoding="utf-8")
    baseline = evaluate(baseline_dir)
    scope = ["solve.py"]
    cfg = dict(sources=sources, baseline=baseline, task=task, task_id="ahc026", metric="official_score",
               direction="higher", evaluation_protocol=dict(public_seed=0,language="python"))
    if part != "switches":
        return sample_search(part, **cfg, allowed_scope=scope, baseline_parameters={"target_stack_offset":1},
                             B=4 if part=="independent" else 10, T=2, K=3, R=1, output=target,
                             evaluate=evaluate, model=plain, controller_model=structured, review_model=structured)
    target.mkdir()
    common = dict(model=plain, review=partial(review_edit,structured), evaluate=evaluate, budget=5,
                  branches=3, islands=2)
    for name in ("aira_mcts","openevolve"):
        run_search(name, **cfg, **common, task_system="Editable scope: " + json.dumps(scope),
                   output=target/name)
    for name, checkpoint in [
        ("openevolve",tree/"s0-multi_ar"/"run"/"checkpoint.json"),
        ("aira_mcts",target/"openevolve"/"checkpoint.json"),
        ("ar",target/"aira_mcts"/"checkpoint.json"),
    ]:
        resume_search(checkpoint, **common, method=name, history_mode="inherit", output=target/("to-"+name))
    return dict(executed_candidate_slots=25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("part", choices=["independent","tree","switches"])
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--relay", type=Path)
    args = parser.parse_args()
    result = run_part(args.part, args.output_root, args.relay)
    print(json.dumps({"part":args.part,"executed_candidate_slots":result["executed_candidate_slots"]}))
