"""Validate a whole model response before publishing its source snapshot."""
import ast
import difflib
import re
from pathlib import PureWindowsPath

from .upstream import SafetyChecks


class EditError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.feedback = dict(code=code, message=message, **details)


def replace_block(content, search, replacement):
    lines = content.split("\n")
    pattern = search.split("\n")
    if not search.strip():
        raise EditError("empty_search", "Supply a non-empty SEARCH block.")
    exact = []
    shifted = []
    for i in range(len(lines) - len(pattern) + 1):
        window = lines[i:i + len(pattern)]
        if window == pattern:
            exact.append((i, "", ""))
            continue
        pairs = [(a, b) for a, b in zip(pattern, window) if a.strip() or b.strip()]
        if not all(a.lstrip(" \t") == b.lstrip(" \t") for a, b in pairs):
            continue
        a, b = pairs[0]
        ap = a[:len(a)-len(a.lstrip(" \t"))]
        bp = b[:len(b)-len(b.lstrip(" \t"))]
        if bp.endswith(ap):
            remove, add = "", bp[:len(bp)-len(ap)]
        elif ap.endswith(bp):
            remove, add = ap[:len(ap)-len(bp)], ""
        else:
            continue
        if all(a.startswith(remove) and add+a[len(remove):] == b for a,b in pairs):
            shifted.append((i, remove, add))
    hits = exact or shifted
    if len(hits) != 1:
        if hits:
            raise EditError("ambiguous_search", "Add more surrounding lines.", lines=[i+1 for i,_,_ in hits[:8]])
        anchor = max(pattern, key=lambda x:len(x.strip())).strip()
        near = difflib.get_close_matches(anchor,[x.strip() for x in lines], n=1, cutoff=0.3)
        hint = ""
        if near:
            i = next(i for i,x in enumerate(lines) if x.strip()==near[0])
            hint = "\n".join(lines[max(0,i-2):i+3])[:1200]
        raise EditError("search_not_found", "Copy SEARCH text from the current source.", nearby_source=hint)
    i, remove, add = hits[0]
    repl = replacement.split("\n") if replacement else []
    if any(s.strip() and not s.startswith(remove) for s in repl):
        raise EditError("replacement_indent", "Replacement cannot use the matched indentation shift.")
    lines[i:i+len(pattern)] = [add+s[len(remove):] if s.strip() else s for s in repl]
    return "\n".join(lines)


def apply_response(response, sources):
    staged = dict(sources)
    lines = response.replace("\r\n","\n").splitlines()
    index, path, blocks = 0, None, 0
    pending_header = False

    def until(marker):
        nonlocal index
        start = index
        while index < len(lines) and lines[index] != marker:
            index += 1
        if index == len(lines):
            raise EditError("malformed_block", "Missing delimiter.", delimiter=marker, file=path)
        index += 1
        return "\n".join(lines[start:index-1])

    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        if line.startswith("### FILE: "):
            if pending_header:
                raise EditError("empty_file_edit", "Expected an edit after FILE header.", file=path)
            path = line[10:].strip()
            pending_header = True
            if path not in sources:
                raise EditError("unknown_target", "Use an exact target name.", file=path)
            continue
        if path is None:
            raise EditError("malformed_response", "Expected a FILE header.")
        if line == "<<<<<<< SEARCH":
            search = until("=======")
            replacement = until(">>>>>>> REPLACE")
            try:
                staged[path] = replace_block(staged[path], search, replacement)
            except EditError as exc:
                exc.feedback.update(file=path, block=blocks+1)
                raise
        elif re.fullmatch(r"\x60{3}[\w+#.-]*", line):
            staged[path] = until(chr(96)*3) + "\n"
        else:
            raise EditError("malformed_response", "Expected a fenced file or SEARCH block.", file=path)
        blocks += 1
        pending_header = False
    if pending_header:
        raise EditError("empty_file_edit", "Expected an edit after FILE header.", file=path)
    if not blocks:
        raise EditError("empty_edit", "No complete file or SEARCH block was supplied.")
    for name, code in staged.items():
        if code != sources[name] and name.endswith(".py"):
            try:
                ast.parse(code)
            except SyntaxError as exc:
                raise EditError("syntax_error",str(exc),file=name,line=exc.lineno) from exc
            safe, violations = SafetyChecks._check_code_safety(code)
            if not safe:
                raise EditError("blocked_edit","Source violates editor rules.",file=name,violations=violations)
    return staged


def validate_names(sources):
    names = set()
    for name in sources:
        path = PureWindowsPath(name)
        if (not path.parts or path.drive or path.root or "\\" in name or
                path.as_posix()!=name or ".." in path.parts or ":" in name or
                path.is_reserved() or path in names or any(x.endswith((" ",".")) for x in path.parts)):
            raise ValueError(f"Invalid or duplicate target: {name}")
        names.add(path)


def edit_prompt(task, instruction, sources):
    files = "\n\n".join(f"### FILE: {name}\n{text}" for name,text in sources.items())
    return (f"## Task Context\n{task}\n\n## Instruction\n{instruction}\n\n## Target files\n{files}\n\n"
            "Make targeted, minimal changes. Preserve unrelated behavior. Use exact target names. "
            "Return only edited files, each starting with '### FILE: <name>'. "
            "Use a fenced complete file for small sources or line-delimited SEARCH/REPLACE blocks "
            "for larger sources. Block delimiters are <<<<<<< SEARCH, =======, and >>>>>>> REPLACE.")
