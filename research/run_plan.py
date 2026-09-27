from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

import cplex

from evacuation.data import Settings, build_instance
from evacuation.solver import solve, write_json
from evacuation.policies import solve_policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    root = Path(__file__).parent
    source_hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted((root / "evacuation").glob("*.py"))}
    plan_hash = hashlib.sha256(args.plan.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    progress_file = args.output / "progress.json"
    state = {"pid": os.getpid(), "purpose": plan["purpose"], "plan_sha256": plan_hash,
             "source_hashes": source_hashes, "python": sys.executable, "platform": platform.platform(),
             "solver_version": cplex.__version__, "completed": [], "active": None, "failures": []}
    for entry in plan["runs"]:
        target = args.output / entry["id"]
        if (target / "result.json").exists():
            provenance = json.loads((target / "provenance.json").read_text(encoding="utf-8"))
            if provenance["source_hashes"] != source_hashes or provenance["plan_sha256"] != plan_hash:
                raise RuntimeError(f"Changed code or plan for completed run {entry['id']}")
            state["completed"].append(entry["id"])
            continue
        state["active"] = entry["id"]
        write_json(progress_file, state)
        print(json.dumps({"start": entry["id"], "purpose": plan["purpose"], "limit": entry["seconds"]}), flush=True)
        target.mkdir(parents=True, exist_ok=True)
        write_json(target / "provenance.json", {"source_hashes": source_hashes, "plan_sha256": plan_hash,
                   "entry": entry, "purpose": plan["purpose"], "python": sys.version,
                   "platform": platform.platform(), "started_unix": time.time()})
        try:
            instance = build_instance(Settings(**entry["settings"]))
            if entry.get("policy", "proposed") == "proposed":
                result = solve(instance, entry["algorithm"], entry["seconds"], target)
            else:
                coverage = None
                if entry["policy"] == "controlled":
                    reference_dir = (root / "runs" / entry["reference_run"] if "reference_run" in entry
                                     else args.output / entry["reference"])
                    reference = json.loads((reference_dir / "result.json").read_text(encoding="utf-8"))
                    if reference["instance_sha256"] != instance.digest():
                        raise ValueError("Controlled benchmark does not share the exact proposed instance")
                    coverage = reference["metrics"]["served"]
                result = solve_policy(instance, entry["policy"], entry["seconds"], target, coverage)
            print(json.dumps({"finished": entry["id"], "seconds": result["total_seconds"],
                              "z": (result.get("metrics") or {}).get("z"), "certified": result["certified"]}), flush=True)
            state["completed"].append(entry["id"])
        except Exception as exc:
            state["failures"].append({"id": entry["id"], "error": repr(exc)})
            write_json(progress_file, state)
            raise
    state["active"] = None
    state["finished"] = True
    write_json(progress_file, state)


if __name__ == "__main__":
    main()
