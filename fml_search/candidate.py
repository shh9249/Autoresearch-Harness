"""A single candidate slot and its bounded, local implementation repairs."""
import json
import math
import os
from tempfile import TemporaryDirectory
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from time import perf_counter, sleep
from http.client import HTTPException

from .editor import EditError, apply_response, edit_prompt, validate_names


class APIError(RuntimeError):
    """Transport, provider or authentication failure; independent of candidate repair."""


class CandidateFailure(Exception):
    """A failure of the evaluated source, e.g. compilation, runtime or invalid output."""
    def __init__(self, code, message):
        self.feedback = dict(code=code, message=message)
        super().__init__(message)


def deepseek(role, prompt, *, retries=2, json_output=False):
    key = os.environ["DEEPSEEK_API_KEY"]
    payload = dict(model="deepseek-flash", thinking={"type":"disabled"}, stream=False,
                   messages=[dict(role="system",content=role),dict(role="user",content=prompt)])
    if json_output:
        payload.update(response_format={"type": "json_object"}, max_tokens=2048)
    req = Request("https://api.deepseek.com/chat/completions", data=json.dumps(payload).encode(),
                  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
    for attempt in range(retries+1):
        try:
            with urlopen(req, timeout=120) as response:
                return json.load(response)["choices"][0]["message"]["content"] or ""
        except (HTTPError, URLError, TimeoutError, HTTPException) as exc:
            transient = not isinstance(exc, HTTPError) or exc.code in (408,429,500,502,503,504)
            if not transient or attempt == retries:
                raise APIError(f"Model request failed ({getattr(exc,'code',type(exc).__name__)}).") from exc
            sleep(2**attempt)


def deepseek_json(role, prompt):
    return deepseek(role, prompt, json_output=True)


def write_sources(directory, sources):
    with TemporaryDirectory(dir=directory.parent) as temporary:
        staged = Path(temporary) / "sources"
        for name, text in sources.items():
            dest = staged / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        for attempt in range(4):
            try:
                staged.rename(directory)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32) or directory.exists() or attempt == 3:
                    raise
                sleep(0.05 * 2**attempt)


def run_candidate(*, slot, parent, group, task, proposal, sources, directory, model,
                  evaluate, max_repairs, effective, candidate_id, parent_id, proposal_error=None,
                  review=None):
    """evaluate(source_directory) returns a raw metric or raises CandidateFailure."""
    validate_names(sources)
    root = Path(directory)/f"slot-{slot:05d}"
    root.mkdir(parents=True,exist_ok=False)
    record = dict(slot=slot,id=candidate_id,parent=parent,parent_id=parent_id,group=group,
                  proposal=proposal,attempts=[],metric=None)
    active = dict(sources)

    def finish(status):
        record["status"] = status
        if review is not None:
            last = record["attempts"][-1] if record["attempts"] else {}
            record["candidate_status"] = ("valid" if status == "success" else "no-op"
                                          if last.get("failure", {}).get("code") == "no_effect" else "failed")
            record.setdefault("change_summary", "No completed source edit.")
        (root/"result.json").write_text(json.dumps(record,indent=2,allow_nan=False),encoding="utf-8")
        return record, active

    if not proposal or not proposal.strip():
        record["error"] = proposal_error or "The proposal was empty."
        return finish("proposal_failed")
    feedback = None
    for index in range(max_repairs+1):
        attempt = {"attempt":index}
        record["attempts"].append(attempt)
        instruction = proposal if feedback is None else (
            "Repair the current implementation using the previous attempt's failure feedback.\n"
            +json.dumps(feedback,ensure_ascii=False))
        try:
            prompt = edit_prompt(task, instruction, active)
            started = perf_counter()
            reply = model("You are editing existing source code.", prompt)
            if review is not None:
                attempt["edit_seconds"] = perf_counter() - started
            (root / f"edit-{index}.json").write_text(
                json.dumps(dict(prompt=prompt, response=reply), indent=2), encoding="utf-8")
            staged = apply_response(reply,active)
            unchanged = staged == active or not effective(sources, staged)
            if review is not None:
                started = perf_counter()
                audit = review(proposal, sources, staged)
                attempt["review_seconds"] = perf_counter() - started
                attempt["review"] = audit
                record["change_summary"] = audit["change_summary"]
                active = staged
                if unchanged or audit["verdict"] == "no_op":
                    if staged == sources:
                        record["change_summary"] = "The candidate source is identical to its parent."
                    raise CandidateFailure("no_effect", audit["feedback"] or "Make an effective source change.")
                if audit["verdict"] == "implementation_error":
                    raise CandidateFailure("implementation_error", audit["feedback"])
            elif unchanged:
                raise CandidateFailure("no_effect","The edit did not produce an effective change.")
            active = staged
            dest = root/f"attempt-{index}"
            write_sources(dest,active)
            attempt["evaluation_started"] = True
            started = perf_counter()
            try:
                metric = evaluate(dest)
            finally:
                if review is not None:
                    attempt["evaluation_seconds"] = perf_counter() - started
            if isinstance(metric,bool) or not isinstance(metric,(float,int)) or not math.isfinite(metric):
                raise CandidateFailure("non_finite_metric","Evaluation did not return a finite numeric metric.")
        except (EditError,CandidateFailure) as exc:
            feedback = exc.feedback
            attempt["failure"] = feedback
            continue
        except Exception as exc:
            attempt["error_type"] = type(exc).__name__
            finish("infrastructure_error")
            raise
        record["metric"] = metric
        return finish("success")
    return finish("candidate_failed")
