"""Local file relay for using the model API from a network-restricted evaluator."""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from getpass import getpass
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from .candidate import APIError, deepseek
from .checkpoint import dump


def request(directory, role, prompt, json_output):
    directory = Path(directory)
    if not (directory/"ready").is_file():
        raise APIError("Model relay is unavailable.")
    stem = uuid4().hex
    dest = directory/f"{stem}.reply"
    dump(directory/f"{stem}.request", dict(role=role,prompt=prompt,json_output=json_output))
    deadline = monotonic() + 390
    while monotonic() < deadline:
        if dest.is_file():
            result = json.loads(dest.read_text(encoding="utf-8"))
            if result.get("error"):
                raise APIError(result["error"])
            return result["content"]
        sleep(0.2)
    raise APIError("Model relay request timed out.")


def serve(directory, seconds):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["DEEPSEEK_API_KEY"] = getpass("DeepSeek API key (hidden): ").strip()
    if not os.environ["DEEPSEEK_API_KEY"]:
        raise ValueError("Missing DeepSeek API key")
    (directory/"ready").touch()
    deadline = monotonic() + seconds

    def reply(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data["role"], str) or not isinstance(data["prompt"], str)
                    or type(data["json_output"]) is not bool):
                raise ValueError("Invalid relay request.")
            result = dict(content=deepseek(data["role"], data["prompt"], json_output=data["json_output"]))
        except (APIError, ValueError, OSError, KeyError) as exc:
            result = dict(error=str(exc))
        dump(path.with_suffix(".reply"), result)
        path.unlink()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            active = {}
            while monotonic() < deadline and not (directory/"stop").exists():
                for path, future in list(active.items()):
                    if future.done():
                        future.result()
                        del active[path]
                for path in sorted(directory.glob("*.request")):
                    if len(active) == 4:
                        break
                    if path not in active:
                        active[path] = pool.submit(reply,path)
                sleep(0.15)
    finally:
        (directory/"ready").unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    serve(args.directory, args.seconds)
