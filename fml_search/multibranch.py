"""Native multibranch segment state and branch-ranked OpenEvolve import."""
from types import SimpleNamespace

from . import upstream as u


def branch_id(row):
    origin = row["provenance"]
    return origin.get("branch", origin.get("group"))


def entry(history, heads, journal, shared, baseline_step, reference):
    """Capture the boundary once. Later checkpoints replay only committed rows."""
    return dict(start=history.cutoff, baseline_step=baseline_step, original_reference=reference,
                shared=shared, journal=[vars(item).copy() for item in journal],
                branches=[dict(seed=h.head, seed_description=h.parent_idea,
                               history=[vars(item).copy() for item in h.history]) for h in heads])


def context(history, count=None):
    saved = history.multi_context
    if saved and saved["start"] <= history.cutoff:
        if count is not None and count != len(saved["branches"]):
            raise ValueError("An uninterrupted multibranch continuation must retain its branch count.")
        return saved
    # Older public checkpoints do not store the initial private state.
    rows = history.rows[1:]
    boundary = max((r["step"] for r in rows if r["provenance"].get("algorithm", "multi_ar") != "multi_ar"),
                   default=0)
    rows = [r for r in rows if r["step"] > boundary]
    first = {}
    for row in rows:
        group = branch_id(row)
        if type(group) is not int or group < 0:
            raise ValueError("Multibranch history requires a nonnegative source branch ID.")
        first.setdefault(group, history.by_id[row["parent_id"]])
    size = count if count is not None else max(first, default=0) + 1
    if type(size) is not int or size <= max(first, default=-1):
        raise ValueError("The branch count omits a recorded branch.")
    seeds = [first.get(i, history.root) for i in range(size)]
    return dict(start=boundary, baseline_step=0, original_reference=False, shared="", journal=[],
                branches=[dict(seed=r["step"], seed_description=r["description"], history=[]) for r in seeds])


def events(history, state):
    for row in history.rows:
        if row["step"] <= state["start"]:
            continue
        group = branch_id(row)
        if type(group) is not int or not 0 <= group < len(state["branches"]):
            raise ValueError("Candidate has no matching native branch.")
        yield group, row


def restore(history, state, *, load_codes=True):
    heads = []
    for i, saved in enumerate(state["branches"]):
        row = history.by_step[saved["seed"]]
        heads.append(SimpleNamespace(branch_id=i, head=row["step"], best_metric=row["score"],
                                     parent_step_id=row["step"], parent_metric=row["score"],
                                     parent_idea=saved["seed_description"],
                                     history=[SimpleNamespace(**r) for r in saved["history"]]))
    journal = [u.JournalEntry(**r) for r in state["journal"]]
    cursor, wave_start, siblings = 0, len(journal), []
    for group, row in events(history, state):
        head = heads[group]
        if group == 0:
            wave_start, siblings = len(journal), []
        if not head.history and row["description"]:
            siblings.append((group, row["description"]))
        value = row["score"]
        keep = value is not None and (value > head.best_metric if history.meta["direction"] == "higher"
                                      else value < head.best_metric)
        status = row["provenance"].get("selection") or (
            ("keep" if keep else "discard") if value is not None else
            row["provenance"].get("status", "crash"))
        if keep:
            head.head, head.best_metric = row["step"], value
        head.history.append(SimpleNamespace(step_id=row["step"], primary_metric=value, status=status,
                                            description=row["description"] or str(row.get("error") or "")))
        journal.append(u.JournalEntry(group, row["step"], status, value, row["description"]))
        cursor = (group + 1) % len(heads)
    if load_codes:
        for head in heads:
            head.code = history.sources(history.by_step[head.head])
    return heads, journal, cursor, wave_start if cursor else len(journal), siblings if cursor else []


def branch_population(history, db):
    state = context(history)
    groups = {}
    for group, saved in enumerate(state["branches"]):
        rows = {r["step_id"] for r in saved["history"]}
        groups[group] = {saved["seed"], *rows}
    for group, row in events(history, state):
        groups[group].add(row["step"])
    maximize = history.meta["direction"] == "higher"
    choose = max if maximize else min
    def best_step(steps):
        valid = [s for s in sorted(steps) if history.by_step[s]["score"] is not None]
        return choose(valid, key=lambda s: history.by_step[s]["score"])
    best = {g: best_step(steps) for g, steps in groups.items()}
    order = sorted(groups, key=lambda g: history.by_step[best[g]]["score"], reverse=maximize)
    manifest = []
    references = []
    db.source_failures = {}
    for rank, group in enumerate(order):
        active = rank < db.num_islands
        steps = sorted(groups[group])
        manifest.append(dict(branch=group, role="active" if active else "reference",
                             island=rank if active else None, steps=steps, best_step=best[group]))
        if not active:
            row = history.by_step[best[group]]
            references.append(f"Branch {group}, best step {row['step']}, {history.meta['metric']}="
                              f"{row['score']}: {row['description']}")
            continue
        imported = {}
        failures = []
        for step in steps:
            row = history.by_step[step]
            parent = imported.get(row["parent_id"])
            if row["score"] is None:
                failures.append(f"Step {step}: {row['description']} | {row['error']}")
                imported[row["id"]] = parent
                continue
            code = history.sources(row)
            program = u.Program(code_snapshot=code, island=rank, step_id=step,
                                parent_id=parent.id if parent else None,
                                generation=parent.generation+1 if parent else 0,
                                action="evolve" if parent else "seed", primary_metric=row["score"],
                                is_buggy=False, changes_description=row["description"],
                                complexity=u.compute_complexity(code),
                                diversity=u.compute_diversity(code, db._reference_programs))
            db.add(program, iteration=step)
            imported[row["id"]] = program
        db.island_generations[rank] = sum(s > state["start"] for s in steps)
        if failures:
            db.source_failures[rank] = "\n\n## Failed source attempts on this island\n" + "\n".join(failures[-8:])
    db.last_migration_gen = max(db.island_generations)
    db.branch_import = manifest
    db.reference_summary = ("\n\n## Other source branches (reference only)\n" + "\n".join(references)
                            if references else "")
    return db
