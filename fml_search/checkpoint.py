"""Portable public history shared by every search method."""
import json
import math
import os
from pathlib import Path

from .editor import validate_names


def dump(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                         encoding="utf-8")
    temporary.replace(path)


def candidate_row(cid, parent, step, code_path, score, description, *, error=None,
                  action="improve", provenance=None):
    return dict(id=cid, parent_id=parent, step=step, code_path=code_path, score=score,
                description=description, error=error, action=action, provenance=provenance or {})


def valid_id(value):
    return type(value) is int or isinstance(value, str) and bool(value)


class History:
    def __init__(self, data, directory):
        self.directory = Path(directory).resolve()
        self.meta = {key: data[key] for key in (
            "task", "metric", "direction", "task_description", "evaluation_protocol",
            "source", "task_system")}
        self.cutoff = data["cutoff"]
        self.ar_context = data.get("ar_context")
        self.multi_context = data.get("multi_context")
        self.aira_context = data.get("aira_context")
        self.oe_states = data.get("oe_states", {})
        if type(self.cutoff) is not int or self.cutoff < 0:
            raise ValueError("cutoff must be a nonnegative integer")
        if self.meta["direction"] not in ("higher", "lower"):
            raise ValueError("direction must be higher or lower")
        if not isinstance(self.meta["task_system"], str):
            raise ValueError("task_system must be a string")
        visible = []
        for row in data["candidates"]:
            step = row["step"]
            if type(step) is not int or step < 0:
                raise ValueError("Candidate step must be a nonnegative integer")
            if step <= self.cutoff:
                visible.append(dict(row))
        self.rows = []
        aliases = {}
        self.by_id = {}
        self.by_step = {}
        for row in sorted(visible, key=lambda r: r["step"]):
            cid, parent, step = row["id"], row["parent_id"], row["step"]
            if not valid_id(cid) or cid in self.by_id or cid in aliases:
                raise ValueError("Invalid or duplicate candidate identity or step")
            origin = row.get("provenance", {})
            if origin.get("native_action", row.get("action")) in ("migrate", "migration"):
                source = origin.get("original_candidate_id", parent)
                source = aliases.get(source, source)
                if source not in self.by_id:
                    raise ValueError(f"Migration copy {cid!r} has no visible original candidate")
                aliases[cid] = source
                continue
            row["parent_id"] = parent = aliases.get(parent, parent)
            if step in self.by_step:
                raise ValueError("Duplicate evaluated step")
            if step == 0 and parent is not None or step > 0 and (
                    not valid_id(parent) or parent not in self.by_id):
                raise ValueError(f"Expected an earlier visible parent for candidate {cid!r}")
            score = row["score"]
            if score is not None and (type(score) not in (float, int) or not math.isfinite(score)):
                raise ValueError("Use a finite raw score or null for failure")
            if score is not None and not row.get("code_path"):
                raise ValueError("A scored candidate needs a code path")
            row.setdefault("error", None)
            row.setdefault("description", "")
            row.setdefault("action", "improve")
            row.setdefault("provenance", {})
            if not isinstance(row["provenance"], dict) or not isinstance(row["description"], str):
                raise ValueError("Invalid provenance or description")
            self.by_id[cid] = row
            self.by_step[step] = row
            self.rows.append(row)
        if 0 not in self.by_step or self.by_step[0]["score"] is None:
            raise ValueError("Expected a scored original baseline at step zero")
        self.root = self.by_step[0]
        best = max if self.meta["direction"] == "higher" else min
        self.best = best((r for r in self.rows if r["score"] is not None), key=lambda r: r["score"])

    @classmethod
    def read(cls, path):
        path = Path(path)
        return cls(json.loads(path.read_text(encoding="utf-8-sig")), path.parent)

    def sources(self, row):
        """Load a source file, source-only directory, or explicit multi-file snapshot."""
        path = self.directory / row["code_path"]
        if path.is_dir():
            files = {f.relative_to(path).as_posix(): f.read_text(encoding="utf-8")
                     for f in sorted(path.rglob("*")) if f.is_file()}
        elif path.name.endswith(".sources.json"):
            files = json.loads(path.read_text(encoding="utf-8"))
        else:
            files = {path.name: path.read_text(encoding="utf-8")}
        if not isinstance(files, dict) or not files or not all(isinstance(v, str) for v in files.values()):
            raise ValueError(f"Invalid source snapshot for {row['id']!r}")
        validate_names(files)
        return files


class Writer:
    def __init__(self, history, output, method, ar_context=None, multi_context=None, aira_context=None):
        self.output = Path(output).resolve()
        self.path = self.output / "checkpoint.json"
        self.method = method
        self.meta = {**history.meta, "source": method}
        if ar_context is not None:
            self.meta["ar_context"] = ar_context
        if multi_context is not None:
            self.meta["multi_context"] = multi_context
        if aira_context is not None:
            self.meta["aira_context"] = aira_context
        self.rows = []
        self.ids = {step: row["id"] for step, row in history.by_step.items()}
        self.used_ids = set(self.ids.values())
        for row in history.rows:
            copy = dict(row)
            if row.get("code_path"):
                path = (history.directory / row["code_path"]).resolve()
                try:
                    copy["code_path"] = Path(os.path.relpath(path, self.output)).as_posix()
                except ValueError:  # Windows volumes may differ.
                    copy["code_path"] = str(path)
            self.rows.append(copy)
        if method == "openevolve":
            self.meta["oe_states"] = {}
            for step, value in history.oe_states.items():
                if int(step) < history.cutoff:
                    path = (history.directory / value).resolve()
                    try:
                        path = Path(os.path.relpath(path, self.output))
                    except ValueError:
                        pass
                    self.meta["oe_states"][step] = path.as_posix()
        else:
            self.publish(history.cutoff)

    def new_id(self, step):
        # Existing producers may use integer IDs unrelated to their step numbers.
        cid = step
        while cid in self.used_ids:
            cid = f"step-{cid}"
        return cid

    def append(self, record, code, native_action, group):
        step = record["slot"]
        cid = record["id"]
        base = Path(f"slot-{step:05d}")
        snapshot = None
        if record["metric"] is not None or any(a.get("evaluation_started") for a in record["attempts"]):
            snapshot = (base / "candidate.sources.json").as_posix()
            dump(self.output / snapshot, code)
        error = record.get("error")
        if record["status"] == "candidate_failed":
            error = record["attempts"][-1]["failure"]
        provenance = dict(algorithm=self.method, native_action=native_action,
                          status=record["status"], group=group)
        provenance["evaluated"] = any(a.get("evaluation_started") for a in record["attempts"])
        if self.method == "multi_ar":
            provenance["branch"] = group
        elif self.method == "openevolve":
            provenance["island"] = group
        for name in ("selection", "analysis", "expansion_remaining", "stage", "round_idx",
                     "change_summary", "candidate_status"):
            if name in record:
                provenance[name] = record[name]
        self.rows.append(candidate_row(cid, record["parent_id"], step, snapshot, record["metric"],
                                       record["proposal"], error=error, provenance=provenance))
        self.ids[step] = cid
        self.used_ids.add(cid)
        self.publish(step)

    def publish(self, cutoff):
        dump(self.path, dict(**self.meta, candidates=self.rows, cutoff=cutoff))
