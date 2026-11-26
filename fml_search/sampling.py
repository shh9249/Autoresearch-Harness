"""Run independent controlled paths or exhaustively branch over configuration sequences."""
import json
from functools import partial
from pathlib import Path

from .candidate import deepseek, deepseek_json
from .checkpoint import History, candidate_row, dump
from .control import CONFIGS, decide, observation, review_edit
from .editor import validate_names
from .search import resume_search, run_search


def sample_search(mode, *, sources, baseline, task, allowed_scope, baseline_parameters,
                  output, evaluate, metric="score", direction="higher", B=100, T=4, K=5, R=1,
                  max_repairs=2, task_id=None, task_system="", evaluation_protocol=None,
                  model=deepseek, controller_model=deepseek_json, review_model=deepseek_json):
    """Produce one manifest plus immutable per-path observations, actions and checkpoints."""
    if mode not in ("independent", "tree"):
        raise ValueError("mode must be independent or tree")
    if any(type(v) is not int or v < 1 for v in (B,T,K,R)) or T > B:
        raise ValueError("B, T, K and R must be positive integers with T <= B.")
    if type(max_repairs) is not int or max_repairs < 0:
        raise ValueError("max_repairs must be a nonnegative integer.")
    if not sources:
        raise ValueError("Provide editable source files.")
    validate_names(sources)
    out = Path(output)
    bounds = [B*n//T for n in range(T+1)]
    initial = candidate_row(0,None,0,"baseline.sources.json",baseline,"Original baseline",action="baseline")
    task_system = (task_system + "\nEditable scope: " + json.dumps(allowed_scope, ensure_ascii=False)).strip()
    meta = dict(task=task_id or task, metric=metric, direction=direction, candidates=[initial], cutoff=0,
                source="baseline", task_description=task, evaluation_protocol=evaluation_protocol,
                task_system=task_system)
    root = History(meta, out)
    manifest = dict(mode=mode, B=B, T=T, K=K, R=R, boundaries=bounds,
                    completed_edges=0, executed_candidate_slots=0, leaves=[])
    out.mkdir(parents=True, exist_ok=False)
    dump(out/"baseline.sources.json", sources)
    dump(out/"checkpoint.json", meta)
    dump(out/"sampling.json", manifest)
    common = dict(evaluate=evaluate, model=model, review=partial(review_edit, review_model),
                  branches=K, max_repairs=max_repairs)

    def traverse(prefix, checkpoint, history, choices, repeat):
        stage = len(choices)
        if stage == T:
            final = observation(history, choices, bounds, K, allowed_scope, baseline_parameters)
            dump(prefix/"path.json", final)
            manifest["leaves"].append(dict(repeat=repeat, configs=list(choices),
                                           checkpoint=checkpoint.relative_to(out).as_posix(),
                                           path=(prefix/"path.json").relative_to(out).as_posix(),
                                           best_id=history.best["id"], best_score=history.best["score"]))
            dump(out/"sampling.json", manifest)
            return
        obs = observation(history, choices, bounds, K, allowed_scope, baseline_parameters)
        dump(prefix/"observation.json", obs)
        if mode == "independent":
            decisions = [decide(controller_model, obs)]
        else:
            decisions = [dict(config_id=name, reason="Exhaustive program enumeration.") for name in CONFIGS]
        for decision in decisions:
            name = decision["config_id"]
            node = prefix / f"s{stage}-{name}"
            node.mkdir()
            dump(node/"decision.json", dict(source="controller" if mode=="independent" else "enumeration",
                                            **decision))
            segment = node/"run"
            kwargs = dict(method=name, budget=bounds[stage+1]-bounds[stage], output=segment,
                          stage_index=stage, **common)
            if stage == 0:
                records = run_search(sources=sources, baseline=baseline, task=task, task_id=task_id,
                                     task_system=task_system, evaluation_protocol=evaluation_protocol,
                                     metric=metric, direction=direction, **kwargs)
            else:
                records = resume_search(checkpoint, history_mode="inherit", **kwargs)
            child_checkpoint = segment/"checkpoint.json"
            child = History.read(child_checkpoint)
            if child.cutoff != bounds[stage+1] or len(records) != kwargs["budget"]:
                raise RuntimeError("Search exceeded or fell short of its stage budget.")
            manifest["executed_candidate_slots"] += len(records)
            manifest["completed_edges"] += 1
            dump(out/"sampling.json", manifest)
            traverse(node, child_checkpoint, child, (*choices, name), repeat)

    for repeat in range(R):
        prefix = out/f"rep-{repeat:03d}"
        prefix.mkdir()
        traverse(prefix, out/"checkpoint.json", root, (), repeat)
    return manifest
