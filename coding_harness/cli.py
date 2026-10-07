import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from coding_harness.agent import run_agent
from coding_harness.model import OllamaModel
from coding_harness.runtime import Runtime
from coding_harness.results import export_results, snapshot_files

PACKAGE_DIR = Path(__file__).resolve().parents[1]

OLLAMA_MODEL = "qwen2.5-coder"
MODEL_MAX_TURNS = 15
MODEL_NUM_CTX = 16384

def build_parser():
    parser = argparse.ArgumentParser(description="Run the POC coding harness.")
    parser.add_argument("--root", required=True, help="target workspace")
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-turns", type=int, default=MODEL_MAX_TURNS)
    return parser

def ask_approval(command, cwd):
    """Human approval for one bash command; only an exact 'y' approves."""
    print(f"\n[bash request]\n  command: {command}\n  cwd:     {cwd}", file=sys.stderr)
    try:
        answer = input("Approve this command only? y/n: ")
    except EOFError:
        answer = ""
    return answer == "y"


def submit_task(task):
    return

def show_results(args):
    return

def main():
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"--root is not a directory: {root}")
    run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    trace = (PACKAGE_DIR / "traces" / f"{run_id}.jsonl").resolve()
    artifacts = trace.parent / run_id
    trace.parent.mkdir(parents=True, exist_ok=True)

    with trace.open("a", encoding="utf-8") as log:

        def emit(event):
            payload = json.dumps(event, default=str)

            log.write(
                json.dumps(
                    {"time": time.time(), **event},
                    default=str,
                ) + "\n"
            )
            log.flush()

            print(
                f"[{event.get('type')}] {payload[:400]}",
                file=sys.stderr,
            )

        def approve(command, cwd):
            approved = ask_approval(command, cwd)

            emit({
                "type": "approval",
                "command": command,
                "cwd": cwd,
                "approved": approved,
            })

            return approved

        emit({
            "type": "start",
            "root": str(root),
            "task": args.task,
        })

        ollama = OllamaModel(
            os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b"),
            num_ctx=MODEL_NUM_CTX,
        )

        def model(messages):
            reply = ollama(messages)

            emit({
                "type": "usage",
                **ollama.last_usage,
            })

            return reply

        with Runtime(root, approve=approve) as runtime:
            before = snapshot_files(runtime.root)
            emit({"type": "snapshot", "workspace": str(runtime.root)})
            outcome = run_agent(
                model, runtime, args.task,
                max_turns=args.max_turns, emit=emit,
            )
            changes = export_results(before, runtime.root, artifacts)
            emit({"type": "artifacts", "directory": str(artifacts), "changes": changes})

    termination = outcome["termination"]

    print(f"\ntermination: {termination}")

    if "final" in outcome:
        # The claim is the model's own text; nothing here has checked it.
        print(f"Model claims (unverified): {outcome['final']}")
    else:
        print(outcome.get("reason", ""))

    print(f"trace: {trace}")
    print(f"results: {artifacts}")
    print("Changed files:")
    for change in changes:
        print(f"  {change['status']}: {change['path']}")
    if not changes:
        print("  (none)")
    print(f"diff: {artifacts / 'changes.diff'}")

    return 0 if termination == "final" else 1

if __name__ == "__main__":
    sys.exit(main())
