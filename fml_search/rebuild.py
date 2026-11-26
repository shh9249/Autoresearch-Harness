"""Target-side reconstruction from a cutoff-limited public history."""
import math
from types import SimpleNamespace

from . import upstream as u
from . import multibranch


def group_for(row, method, count):
    origin = row["provenance"]
    key = "branch" if method == "multi_ar" else "island" if method == "openevolve" else "group"
    group = origin.get(key, origin.get("group"))
    if origin.get("algorithm") == method and type(group) is int and 0 <= group < count:
        return group
    return (row["step"] - 1) % count


def ar_context(history, inherit, fresh):
    if inherit and history.meta["source"] == "ar":
        previous = history.ar_context
        if previous and previous["log_after"] <= history.cutoff:
            return previous
        return dict(baseline_step=0, log_after=0, original_reference=False)
    return dict(baseline_step=0 if inherit else history.best["step"],
                log_after=0 if inherit else history.cutoff, original_reference=not fresh,
                evaluated_only_through=history.cutoff if inherit and history.meta["source"] == "openevolve" else -1,
                native_status_after=history.cutoff)


def replay_ar(history, context):
    incumbent = history.by_step[context["baseline_step"]]
    records, improvements = [], []
    for row in history.rows:
        if row["step"] <= context["log_after"]:
            continue
        if row["step"] <= context.get("evaluated_only_through", -1) and row["provenance"].get("evaluated") is False:
            continue
        score = row["score"]
        keep = score is not None and (score > incumbent["score"] if history.meta["direction"] == "higher"
                                      else score < incumbent["score"])
        fallback = "crash" if score is None else "keep" if keep else "discard"
        status = fallback
        if row["step"] > context.get("native_status_after", 0) and row["provenance"].get("algorithm") == "ar":
            status = row["provenance"].get("selection", fallback)
        records.append(SimpleNamespace(step_id=row["step"], primary_metric=score, status=status,
                                       description=row["description"] or str(row["error"] or "")))
        if keep:
            improvements.append(row)
            incumbent = row
    return records, improvements, incumbent


def ar_heads(history, method, count, inherit, start_code):
    best = history.best
    first = history.root if inherit else best
    heads = [SimpleNamespace(branch_id=i, history=[], head=first["step"], best_metric=first["score"])
             for i in range(count)]
    journal = []
    if inherit and history.meta["source"] in ("multi_ar", "aira_mcts", "openevolve") and method == "ar":
        records, _, row = replay_ar(history, dict(baseline_step=0, log_after=0,
                                                  evaluated_only_through=history.cutoff if history.meta["source"] == "openevolve" else -1,
                                                  native_status_after=history.cutoff))
        heads[0].history = records
        heads[0].head, heads[0].best_metric, heads[0].code = row["step"], row["score"], start_code
        return heads, journal
    if inherit and (history.meta["source"] == "ar" or
                    history.meta["source"] in ("aira_mcts", "openevolve") and method == "multi_ar"):
        context = ar_context(history, True, False) if method == "ar" else dict(baseline_step=0, log_after=0,
            evaluated_only_through=history.cutoff if history.meta["source"] == "openevolve" else -1)
        shared, improvements, incumbent = replay_ar(history, context)
        if method == "ar":
            heads[0].history = shared
            seeds = [incumbent]
        else:
            seeds = list(reversed(improvements[-count:]))
            seeds.extend([history.by_step[context["baseline_step"]]] * (count - len(seeds)))
        for head, row in zip(heads, seeds):
            head.head, head.best_metric = row["step"], row["score"]
            head.code = start_code if row is best else history.sources(row)
            head.parent_step_id, head.parent_metric = head.head, head.best_metric
            head.parent_idea = row["description"]
            head.shared_ar_history = shared
        return heads, journal
    if inherit:
        for row in history.rows[1:]:
            group = group_for(row, method, count)
            head = heads[group]
            score = row["score"]
            keep = score is not None and (score > head.best_metric if history.meta["direction"] == "higher"
                                          else score < head.best_metric)
            status = "crash" if score is None else "keep" if keep else "discard"
            description = row["description"] or str(row["error"] or "")
            if keep:
                head.best_metric, head.head = score, row["step"]
            head.history.append(SimpleNamespace(step_id=row["step"], primary_metric=score,
                                                status=status, description=description))
            journal.append(u.JournalEntry(group, row["step"], status, score, description))
    for head in heads:
        if not head.history:
            head.head, head.best_metric = best["step"], best["score"]
        row = history.by_step[head.head]
        head.code = start_code if row is best else history.sources(row)
        head.parent_step_id, head.parent_metric = head.head, head.best_metric
        head.parent_idea = row["description"] if inherit else "Starting candidate"
    return heads, journal


def aira_context(history, inherit, fresh):
    if inherit and history.meta["source"] == "aira_mcts" and history.aira_context:
        if history.aira_context["log_after"] <= history.cutoff:
            return history.aira_context
    first = history.root if inherit else history.best
    return dict(root_step=first["step"], log_after=0 if inherit else history.cutoff,
                keep_failed_edges_through=(history.cutoff if inherit and history.meta["source"] in
                                           ("ar", "multi_ar", "aira_mcts", "openevolve") else -1),
                evaluated_only_through=history.cutoff if inherit and history.meta["source"] == "openevolve" else -1,
                original_reference=not fresh)


def mcts(history, inherit, start_code):
    state = aira_context(history, inherit, False)
    first = history.by_step[state["root_step"]]
    code = history.sources(first) if inherit and first is not history.best else start_code
    root = u.MCTSNode(code_snapshot=code, step_id=first["step"], primary_metric=first["score"])
    root.node_id = first["id"]
    journal = u.Journal()
    low, high = math.inf, -math.inf
    if inherit:
        usable = {first["id"]: root}
        for row in history.rows[1:]:
            if row["step"] <= state["log_after"]:
                continue
            preserve_tree = row["step"] <= state["keep_failed_edges_through"]
            parent = usable[row["parent_id"]]
            if row["step"] <= state.get("evaluated_only_through", -1) and row["provenance"].get("evaluated") is False:
                usable[row["id"]] = parent
                continue
            score = row["score"]
            node = u.MCTSNode(plan=row["description"], parent=parent, step_id=row["step"],
                              action="draft" if parent is root else "improve",
                              primary_metric=score, is_buggy=score is None,
                              code_snapshot=history.sources(row) if score is not None else None,
                              error_context=row["error"])
            node.analysis = row["provenance"].get("analysis", "")
            node.node_id = row["id"]
            journal.append(node)
            if score is None:
                if preserve_tree:
                    parent.children.append(node)
                usable[row["id"]] = node if preserve_tree else parent
                continue
            usable[row["id"]] = node
            parent.children.append(node)
            low, high = min(low, score), max(high, score)
            ancestor = node
            while ancestor is not None:
                ancestor.increment_explore_count()
                ancestor.add_value(score)
                ancestor = ancestor.parent
    return root, journal, low, high


def lineage_islands(history, count, *, balance_subtrees=False):
    """Partition the scored lineage at forks. Retain failed rows in public history."""
    root_id = history.root["id"]
    nearest = {root_id: root_id}
    children = {root_id: []}
    parents = {}
    for row in history.rows[1:]:
        cid = row["id"]
        parent = parents[cid] = nearest[row["parent_id"]]
        if row["score"] is None:
            nearest[cid] = parent
        else:
            nearest[cid] = cid
            children[parent].append(cid)
            children[cid] = []
    if balance_subtrees:
        islands = {cid: 0 for cid in children}
        depth = {root_id: 0}
        for cid in children:
            if cid != root_id:
                depth[cid] = depth[parents[cid]] + 1
        for new_island in range(1, count):
            # Compute the size still attached to each node after previous cuts.
            sizes = {}
            for cid, kids in reversed(children.items()):
                sizes[cid] = 1 + sum(sizes[k] for k in kids if islands[k] == islands[cid])
            totals = [0] * new_island
            for island in islands.values():
                totals[island] += 1
            cut, priority = None, None
            for cid, kids in children.items():
                siblings = [k for k in kids if islands[k] == islands[cid]]
                if len(siblings) < 2:
                    continue
                for k in siblings:
                    size = sizes[k]
                    gain = size * (totals[islands[cid]] - size)
                    # Prefer intact shallow subtrees. At equal depth maximize
                    # the reduction in sum of squared island populations.
                    key = (-depth[cid], gain)
                    if priority is None or key > priority:
                        cut, priority = k, key
            if cut is None:
                break
            old_island = islands[cut]
            stack = [cut]
            while stack:
                cid = stack.pop()
                islands[cid] = new_island
                stack.extend(k for k in children[cid] if islands[k] == old_island)
        return {cid: islands[ancestor] for cid, ancestor in nearest.items()}
    sizes, main_child = {}, {}
    for cid, siblings in reversed(children.items()):
        sizes[cid] = 1 + sum(sizes[s] for s in siblings)
        if siblings:
            main_child[cid] = max(siblings, key=sizes.__getitem__)
    islands = {root_id: 0}
    next_island = 1
    for row in history.rows[1:]:
        cid = row["id"]
        parent = parents[cid]
        islands[cid] = islands[parent]
        if row["score"] is not None and main_child[parent] != cid and next_island < count:
            islands[cid] = next_island
            next_island += 1
    return islands


def population(history, inherit, start_code, islands=3):
    db = u.ProgramDatabase(metric_direction=history.meta["direction"], num_islands=islands)
    if inherit and history.meta["source"] == "multi_ar":
        return multibranch.branch_population(history, db)
    first = history.root if inherit else history.best
    code = history.sources(first) if inherit and first is not history.best else start_code
    seeds = []
    source = history.meta["source"]
    lineage = (lineage_islands(history, db.num_islands, balance_subtrees=source == "aira_mcts")
               if inherit and source in ("ar", "aira_mcts") else None)
    for island in range(1 if lineage else db.num_islands):
        seed = u.Program(code_snapshot=code, island=island, step_id=first["step"], action="seed",
                         primary_metric=first["score"], is_buggy=False, complexity=u.compute_complexity(code))
        db.add(seed)
        seeds.append(seed)
    if inherit:
        parents = {first["id"]: seeds[0] if lineage else None}
        for row in history.rows[1:]:
            island = lineage[row["id"]] if lineage else group_for(row, "openevolve", db.num_islands)
            parent = parents[row["parent_id"]] or seeds[island]
            if row["score"] is None:
                parents[row["id"]] = parent
            else:
                code = history.sources(row)
                program = u.Program(code_snapshot=code, island=island, step_id=row["step"],
                                    parent_id=parent.id, generation=parent.generation+1, action="evolve",
                                    primary_metric=row["score"], is_buggy=False,
                                    changes_description=row["description"], analysis=row["provenance"].get("analysis", ""),
                                    complexity=u.compute_complexity(code),
                                    diversity=u.compute_diversity(code, db._reference_programs))
                db.add(program, iteration=row["step"])
                parents[row["id"]] = program
            db.island_generations[island] += 1
        db.last_migration_gen = max(db.island_generations)
    return db
