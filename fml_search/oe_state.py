"""Data-only OpenEvolve checkpoints. Source text is shared with the public candidate archive."""
import gzip
import json
from pathlib import Path

from . import upstream as u


def save(writer, step, db, pending, baseline_step, reference):
    fields = dict(vars(db))
    for key in ("programs", "rng", "islands", "archive"):
        del fields[key]
    fields["islands"] = [sorted(group) for group in db.islands]
    fields["archive"] = sorted(db.archive)
    programs = [{key: value for key, value in vars(p).items() if key != "code_snapshot"}
                for p in db.programs.values()]
    data = dict(version=1, step=step, fields=fields, programs=programs, rng=db.rng.getstate(),
                pending=pending, baseline_step=baseline_step, original_reference=reference)
    relative = Path("oe-state") / f"{step:08d}.json.gz"
    path = writer.output / relative
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as stream:
        json.dump(data, stream, separators=(",", ":"), allow_nan=False)
    tmp.replace(path)
    writer.meta["oe_states"][str(step)] = relative.as_posix()


def load(history, requested_islands):
    step = history.cutoff
    name = history.oe_states.get(str(step))
    if name is None:
        raise ValueError(f"Exact OE continuation at step {step} requires its native state sidecar.")
    with gzip.open(history.directory / name, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if data["step"] != step or data["version"] != 1:
        raise ValueError("OE snapshot version or cutoff mismatch")
    fields = data["fields"]
    if requested_islands is not None and requested_islands != fields["num_islands"]:
        raise ValueError("Native OE continuation must retain the saved island count")
    db = u.ProgramDatabase()
    db.__dict__.update(fields)
    db.islands = [set(group) for group in fields["islands"]]
    db.archive = set(fields["archive"])
    if hasattr(db, "source_failures"):
        db.source_failures = {int(k):v for k,v in db.source_failures.items()}
    version, internal, gaussian = data["rng"]
    db.rng.setstate((version, tuple(internal), gaussian))
    code = {}
    for item in data["programs"]:
        step_id = item["step_id"]
        if step_id not in code:
            code[step_id] = history.sources(history.by_step[step_id])
        p = u.Program(code_snapshot=code[step_id], **item)
        db.programs[p.id] = p
    return db, data["pending"], data["baseline_step"], data["original_reference"]
