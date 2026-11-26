"""Standalone FML search configurations over a fixed worker and evaluator."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from . import upstream as u
from .candidate import APIError, deepseek, run_candidate
from .checkpoint import History, Writer, candidate_row, dump
from .editor import validate_names
from . import multibranch, oe_state, rebuild


class Context(u.Prompts):
    def _format_current_code(self):
        return "\n\n".join(f"### {name}\n{code}" for name,code in self.sources.items())


def run_search(method, *, sources, baseline, task, budget, output, evaluate, model=deepseek,
               metric="score", direction="higher", max_repairs=2, branches=3, islands=3,
               task_id=None, task_system="", evaluation_protocol=None, review=None, stage_index=None,
               effective=lambda parent, changed: parent != changed):
    """Run a fresh search. Callbacks can execute concurrently for multi_ar."""
    validate_names(sources)
    if not sources:
        raise ValueError("Provide at least one target.")
    initial = candidate_row(0, None, 0, "baseline.sources.json", baseline, "Original baseline",
                            action="baseline", provenance={"algorithm": method})
    history = History(dict(task=task if task_id is None else task_id, metric=metric, direction=direction,
                           candidates=[initial], cutoff=0, task_description=task, source=method,
                           evaluation_protocol=evaluation_protocol, task_system=task_system), output)
    return _run(method, history, False, sources, budget, output, evaluate, model,
                max_repairs, branches, islands, effective, fresh=True, review=review, stage_index=stage_index)


def resume_search(checkpoint, *, method, history_mode, budget, output, evaluate, model=deepseek,
                  max_repairs=2, branches=None, islands=None, effective=lambda parent, changed: parent != changed,
                  review=None, stage_index=None):
    """Run one segment and publish a new public checkpoint in its own output directory."""
    if history_mode not in ("inherit", "best_only"):
        raise ValueError("history_mode must be inherit or best_only")
    history = History.read(checkpoint)
    return _run(method, history, history_mode == "inherit", history.sources(history.best),
                budget, output, evaluate, model, max_repairs, branches, islands, effective,
                review=review, stage_index=stage_index)


def _run(method, history, inherit, sources, budget, output, evaluate, model,
         max_repairs, branches, islands, effective, fresh=False, review=None, stage_index=None):
    if method not in ("ar","multi_ar","aira_mcts","openevolve"):
        raise ValueError("Unknown search method.")
    baseline = history.best["score"]
    task, metric, direction = (history.meta[k] for k in ("task_description", "metric", "direction"))
    native_multi = method == "multi_ar" and inherit and history.meta["source"] == "multi_ar"
    multi_state = multibranch.context(history, branches) if native_multi else None
    native_oe = method == "openevolve" and inherit and history.meta["source"] == "openevolve"
    if native_oe:
        db, pending, oe_baseline, oe_reference = oe_state.load(history, islands)
        islands = db.num_islands
    elif islands is None:
        islands = 3
    if branches is None:
        branches = len(multi_state["branches"]) if native_multi else 3
    if any(type(x) is not int for x in (budget,max_repairs,branches,islands)) or min(budget,max_repairs)<0 or min(branches,islands)<1:
        raise ValueError("Invalid budget, repair limit or branch count.")
    Path(output).mkdir(parents=True,exist_ok=False)
    if fresh:
        dump(Path(output) / "baseline.sources.json", sources)
    ar_state = rebuild.ar_context(history, inherit, fresh) if method == "ar" else None
    aira_state = rebuild.aira_context(history, inherit, fresh) if method == "aira_mcts" else None
    offset = history.cutoff
    raw_model = model
    def model(role, prompt):
        system = history.meta["task_system"]
        if original_reference:
            original = history.root["score"]
            role += f"\nOriginal baseline reference: {metric} = {original} ({direction} is better)."
        return raw_model(f"{system}\n\n{role}" if system else role, prompt)
    ctx = Context()
    ctx.metric_name,ctx.metric_direction,ctx.task_description = metric,direction,task
    ctx.baseline_primary_metric = history.root["score"] if inherit else baseline
    if ar_state:
        ctx.baseline_primary_metric = history.by_step[ar_state["baseline_step"]]["score"]
    original_reference = ar_state["original_reference"] if ar_state else not fresh
    if native_oe:
        original_reference = oe_reference
        ctx.baseline_primary_metric = history.by_step[oe_baseline]["score"]
    if aira_state:
        original_reference = aira_state["original_reference"]
        ctx.baseline_primary_metric = history.by_step[aira_state["root_step"]]["score"]
    if native_multi:
        original_reference = multi_state["original_reference"]
        ctx.baseline_primary_metric = history.by_step[multi_state["baseline_step"]]["score"]
    if method in ("ar", "multi_ar"):
        count = branches if method == "multi_ar" else 1
        if native_multi:
            heads, journal, cursor, wave_start, siblings = multibranch.restore(history, multi_state)
            shared_background = multi_state["shared"]
        else:
            heads, journal = rebuild.ar_heads(history, method, count, inherit, sources)
            cursor, wave_start, siblings = 0, len(journal), []
            shared_background = ""
            if method == "multi_ar":
                if inherit and history.meta["source"] in ("ar", "aira_mcts", "openevolve"):
                    ctx.experiment_log = heads[0].shared_ar_history
                    label = {"ar":"AR", "aira_mcts":"AIRA", "openevolve":"OE"}[history.meta["source"]]
                    shared_background = f"\n\n## Shared {label} history before branching\n" + ctx._format_experiment_log()
                baseline_step = 0 if inherit else history.best["step"]
                multi_state = multibranch.entry(history, heads, journal, shared_background,
                                                baseline_step, original_reference)
    writer = Writer(history, output, method, ar_state, multi_state, aira_state)
    records = []
    def candidate(slot,parent,group,proposal,code,error=None):
        return run_candidate(slot=slot,parent=parent,group=group,task=task,proposal=proposal,
                sources=code,directory=output,model=model,evaluate=evaluate,max_repairs=max_repairs,
                effective=effective,proposal_error=error,candidate_id=writer.new_id(slot),
                parent_id=writer.ids[parent],review=review)
    def analyze(record):
        last_attempt = [{k:v for k,v in a.items() if not k.endswith("_seconds")}
                        for a in record["attempts"][-1:]]
        prompt = (f"Analyze these experiment results briefly.\n## Task\n{task}\n"
                  f"Plan: {record['proposal'][:300]}\nMetric: {record['metric']}\n"
                  f"Outcome: {record['status']}\nLast attempt: {last_attempt}\n"
                  "In 2-3 sentences explain what worked and suggest a next experiment.")
        try:
            return model("You are an experiment analyst. Be concise and actionable.",prompt)
        except APIError as exc:
            record["analysis_error"] = str(exc)
            return ""
    if method in ("ar","multi_ar"):
        with ThreadPoolExecutor(max_workers=max(1,min(count,budget))) as pool:
            round_idx = 0
            while len(records)<budget:
                batch = heads[cursor:cursor+budget-len(records)]
                ctx.journal = journal[:wave_start]
                plans = []
                for h in batch:
                    ctx.sources,ctx.current_best_metric,ctx.experiment_log = h.code,h.best_metric,h.history
                    if method=="ar":
                        prompt=ctx._build_idea_prompt()
                    elif not h.history:
                        prompt=ctx._build_branch_init_prompt(h,siblings)
                    else:
                        prompt=ctx._build_idea_prompt_for_branch(h)
                    prompt += shared_background
                    try:
                        idea=model("You are an autonomous researcher. Propose one specific experimental idea.",prompt).strip()
                        error=None
                    except APIError as exc:
                        idea,error="",str(exc)
                    if idea and not h.history:
                        siblings.append((h.branch_id,idea))
                    plans.append((offset+len(records)+len(plans)+1,h.head,h.branch_id,idea,h.code,error))
                jobs=[pool.submit(candidate,*p) for p in plans]
                for h,job in zip(batch,jobs):
                    rec,code=job.result()
                    if stage_index is not None:
                        rec.update(stage=stage_index, round_idx=round_idx)
                    score=rec["metric"]
                    keep=(score is not None and (score>h.best_metric if direction=="higher" else score<h.best_metric))
                    status=("keep" if keep else "discard") if score is not None else rec["status"]
                    rec["selection"] = status
                    if keep:
                        h.best_metric,h.code,h.head=score,code,rec["slot"]
                    h.history.append(SimpleNamespace(step_id=rec["slot"],primary_metric=score,status=status,
                                                     description=rec["proposal"] or rec.get("error","")))
                    journal.append(u.JournalEntry(h.branch_id,rec["slot"],status,score,rec["proposal"]))
                    writer.append(rec, code, "propose", h.branch_id)
                    records.append(rec)
                cursor = (cursor + len(batch)) % count
                round_idx += 1
                if cursor == 0:
                    wave_start, siblings = len(journal), []
    elif method=="aira_mcts":
        root, ctx.journal, low, high = rebuild.mcts(history, inherit, sources)
        remaining = 0
        if inherit and history.meta["source"] == method and ctx.journal.nodes:
            last = history.rows[-1]
            remaining = last["provenance"].get("expansion_remaining", 0)
            if remaining:
                node = ctx.journal.nodes[-1].parent
                path = []
                while node is not None:
                    path.append(node)
                    node = node.parent
                path.reverse()
        while len(records)<budget:
            if not remaining:
                path=u.search_policy(root,1.41,high,low,direction)
                remaining = 2
            parent=path[-1]
            for _ in range(min(remaining,budget-len(records))):
                draft=parent is root
                instruction=ctx._draft_prompt(parent) if draft else ctx._improve_prompt(parent)
                rec,code=candidate(offset+len(records)+1,parent.step_id,None,instruction,parent.code_snapshot)
                records.append(rec)
                score=rec["metric"]
                node=u.MCTSNode(plan=instruction,action="draft" if draft else "improve",
                         code_snapshot=code,parent=parent,primary_metric=score,step_id=rec["slot"],
                         is_buggy=score is None)
                node.node_id = rec["id"]
                ctx.journal.append(node)
                node.analysis=analyze(rec)
                rec["analysis"] = node.analysis
                remaining -= 1
                rec["expansion_remaining"] = remaining
                writer.append(rec, code, node.action, None)
                if score is not None:
                    parent.children.append(node)
                    for n in path+[node]:
                        n.increment_explore_count()
                        n.add_value(score)
                    low,high=min(low,score),max(high,score)
    else:
        if not native_oe:
            db = rebuild.population(history, inherit, sources, islands)
            pending = []
            oe_baseline = 0 if inherit else history.best["step"]
        ctx.db = db
        oe_state.save(writer, offset, db, pending, oe_baseline, original_reference)
        writer.publish(offset)
        if hasattr(db, "branch_import"):
            dump(Path(output) / "branch_import.json", db.branch_import)
        while len(records)<budget:
            if not pending:
                for island, members in enumerate(db.islands):
                    if members:
                        p, inspirations = db.sample(island)
                        top = db.get_top_programs(3, p.island)
                        pending.append((island, p.id, [item.id for item in inspirations], [item.id for item in top]))
            island, parent_key, inspiration_keys, top_keys = pending.pop(0)
            p = db.programs[parent_key]
            inspirations = [db.programs[k] for k in inspiration_keys]
            top = [db.programs[k] for k in top_keys]
            slot = offset + len(records) + 1
            instruction = ctx._build_evolution_instruction(p, inspirations, top, slot)
            instruction += getattr(db, "source_failures", {}).get(p.island, "")
            instruction += getattr(db, "reference_summary", "")
            rec, code = candidate(slot, p.step_id, p.island, instruction, p.code_snapshot)
            records.append(rec)
            analysis = analyze(rec)
            rec["analysis"] = analysis
            if rec["metric"] is not None:
                db.add(u.Program(parent_id=p.id,generation=p.generation+1,island=p.island,
                       step_id=slot,action="evolve",code_snapshot=code,is_buggy=False,
                       primary_metric=rec["metric"],analysis=analysis,changes_description=analysis[:150],
                       complexity=u.compute_complexity(code),
                       diversity=u.compute_diversity(code,db._reference_programs)),iteration=slot)
            db.island_generations[island] += 1
            if not pending and max(db.island_generations)-db.last_migration_gen>=db.migration_interval:
                db.migrate_programs()
            oe_state.save(writer, slot, db, pending, oe_baseline, original_reference)
            writer.append(rec, code, "evolve", p.island)
    (Path(output) / "trajectory.json").write_text(
        json.dumps(dict(method=method, budget=budget, baseline=baseline, metric=metric,
                        start_cutoff=offset, history_mode="fresh" if fresh else "inherit" if inherit else "best_only",
                        direction=direction, max_repairs=max_repairs, records=records),
                   indent=2, allow_nan=False), encoding="utf-8")
    return records
