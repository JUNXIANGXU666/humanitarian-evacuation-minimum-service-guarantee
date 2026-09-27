"""Evaluate the fixed paired time-budget rule without importing a solver."""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_SHA256 = "ecccdff34c96df137b8140452117854f68f5508f27c404a5c8b1cbbc29e68413"
RULE_SHA256 = "d60722fc219536678c3b4ab45da1ad8b3feb2348036393a0f4bae4ba8dbaeabd"
PLANS = {
    "core_v1": (96, "d3f109670ec4430cbea862d04e3a202a3ebe44533e870e31e542bf7ed921f94c"),
    "direct_comparison_v1": (48, "90c71ba5110e3ba7a5930f1d3852b242ae416d2b9143853a40669c39d1785289"),
}
FOLLOWUP_NAME = "budget_sensitivity_v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def sha_value(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def assess(snapshot):
    require(snapshot.get("is_final") is True and snapshot.get("status") == "final"
            and snapshot.get("allow_incomplete") is False
            and snapshot.get("all_plans_complete") is True, "Strict completed collection required")
    require(snapshot.get("freeze") == "formal_v1"
            and snapshot.get("collector_sha256") == COLLECTOR_SHA256, "Unreviewed collection")
    require(snapshot.get("selected_plans") == list(PLANS)
            and snapshot.get("planned_runs") == 144, "Expected the 96 core and 48 direct runs")
    summaries = snapshot.get("plans", [])
    require(len(summaries) == 2 and len({p["name"] for p in summaries}) == 2, "Duplicate plan summaries")
    for summary in summaries:
        require(summary["name"] in PLANS, "Unexpected plan summary")
        count, sha = PLANS[summary["name"]]
        require(summary.get("complete") is True and summary.get("sha256") == sha
                and summary.get("planned_runs") == count and summary.get("result_count") == count
                and summary.get("failure_count") == 0, "Incomplete or changed plan")
    rows = snapshot.get("rows", [])
    require(len(rows) == 144 and Counter(r["plan"] for r in rows) ==
            Counter({name: count for name, (count, _) in PLANS.items()}), "Run counts differ from plans")
    indexed = {}
    for row in rows:
        key = row["plan"] + "/" + row["run_id"]
        require(key not in indexed, "Duplicate result key")
        indexed[key] = row
        require(row.get("has_final_result") is True and row.get("record_state") == "result"
                and row.get("progress_completed") is True and row.get("failure") is None,
                "Every planned outcome must be completed")
        require(row.get("plan_sha256") == PLANS[row["plan"]][1]
                and row.get("limit_seconds_per_stage") == 600
                and row.get("policy") == "proposed", "Unexpected short-budget setting")
        require(type(row.get("source_certified")) is bool, "Missing certification status")
        require(sha_value(row.get("instance_sha256")) and sha_value(row.get("result_sha256")),
                "Missing input or result identity")
        expected = "direct" if row["plan"] == "direct_comparison_v1" else "benders_flow"
        require(row.get("algorithm") == expected, "Unexpected comparison algorithm")
    pairs, central = [], {}
    for row in rows:
        if row["plan"] != "direct_comparison_v1":
            continue
        reference = row["planned_entry"].get("paired_benders")
        require(reference in indexed and reference.startswith("core_v1/"), "Missing declared Benders pair")
        other = indexed[reference]
        require(row["settings"] == other["settings"] and
                row["instance_sha256"] == other["instance_sha256"], "Paired instances differ")
        pairs.append((row, other))
        settings = row["settings"]
        if (settings["network"] in ("Anaheim", "Winnipeg")
                and settings["pattern"] == "concentrated" and settings["scarcity"] == 0.8
                and settings["loss"] == 0.0):
            key = (settings["network"], settings["replication"])
            require(key not in central, "Duplicate central comparison")
            central[key] = (row, other)
    require(len(pairs) == 48 and len({b["run_id"] for _, b in pairs}) == 48,
            "Direct comparisons do not form 48 distinct pairs")
    expected_keys = {(network, draw) for network in ("Anaheim", "Winnipeg") for draw in (1, 2, 3)}
    require(set(central) == expected_keys, "The fixed six-instance family is incomplete")
    selected = [row for key in sorted(central) for row in central[key]]
    unresolved = [row["plan"] + "/" + row["run_id"] for row in selected if not row["source_certified"]]
    fields = ("plan", "run_id", "algorithm", "settings", "instance_sha256", "result_sha256",
              "source_certified", "validation_passed", "maxmin_lower_bound", "maxmin_upper_bound",
              "maxmin_relative_gap", "secondary_lower_bound", "secondary_upper_bound",
              "secondary_relative_gap", "secondary_targets", "seconds", "warm_start_seconds")
    decision = {
        "schema_version": 1,
        "rule_sha256": RULE_SHA256,
        "paired_instances_checked": 48,
        "selected_instances": 6,
        "selected_method_runs": 12,
        "short_certified": 12 - len(unresolved),
        "unresolved_short_runs": unresolved,
        "triggered": bool(unresolved),
        "followup_runs": 12 if unresolved else 0,
        "total_formal_runs": 466 if unresolved else 454,
        "maximum_additional_mip_seconds": 86400 if unresolved else 0,
        "comparisons": [{field: copy.deepcopy(row.get(field)) for field in fields} for row in selected],
    }
    plan = None
    if unresolved:
        plan = {
            "purpose": "Paired time-budget sensitivity on the six central large-network instances, with one 3600-second allowance per stage and standard initialisation.",
            "planned_runs": 12,
            "maximum_mip_stage_count": 24,
            "rule_sha256": RULE_SHA256,
            "implementation_freeze": "formal_v1",
            "runs": [],
        }
        for row in selected:
            settings = copy.deepcopy(row["settings"])
            plan["runs"].append({
                "id": f"budget_{settings['network']}_{row['algorithm']}_r{settings['replication']}",
                "settings": settings,
                "algorithm": row["algorithm"],
                "seconds": 3600,
                "paired_short_run": row["plan"] + "/" + row["run_id"],
                "paired_instance_sha256": row["instance_sha256"],
                "family": "time_budget_sensitivity",
            })
    return decision, plan


def load_collector(root):
    path = root / "research/collect_results.py"
    source = path.read_bytes()
    require(digest(source) == COLLECTOR_SHA256, "Collector changed")
    module = ModuleType("_budget_collector")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    require(path.read_bytes() == data, f"Write verification failed: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root, output = ROOT.resolve(), args.output.resolve()
    require(output.is_relative_to(root / "audit") and not output.exists(), "Use a new audit output directory")
    rule = root / "audit/time_budget_review.md"
    require(digest(rule.read_bytes()) == RULE_SHA256, "Time-budget decision rule changed")
    collector = load_collector(root)
    snapshot = collector.collect(root / "research", list(PLANS), allow_incomplete=False)
    decision, plan = assess(snapshot)
    manifest_path = root / "research/frozen/formal_v1/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    for relative, expected in manifest["files"].items():
        path = (root / "research" / relative).resolve()
        require(path.is_relative_to(root / "research") and digest(path.read_bytes()) == expected,
                f"Frozen input changed: {relative}")
    plan_path = root / "research/plans" / (FOLLOWUP_NAME + ".json")
    require(not plan_path.exists(), "A follow-up plan already exists. Inspect it without overwriting.")
    output.mkdir(parents=True, exist_ok=False)
    collector.write_outputs(snapshot, output / "short_budget", root / "research")
    decision.update({
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "tool_sha256": digest(Path(__file__).read_bytes()),
        "freeze_manifest_sha256": digest(manifest_bytes),
        "short_collection": (output / "short_budget/results.json").relative_to(root).as_posix(),
        "short_collection_sha256": digest((output / "short_budget/results.json").read_bytes()),
        "source_hashes": snapshot["source_hashes"],
        "solver_started": False,
    })
    if plan is not None:
        data = encoded(plan)
        write_new(plan_path, data)
        decision["followup_plan"] = plan_path.relative_to(root).as_posix()
        decision["followup_plan_sha256"] = digest(data)
    write_new(output / "decision.json", encoded(decision))
    print(json.dumps({key: decision[key] for key in
                      ("triggered", "short_certified", "followup_runs", "total_formal_runs", "solver_started")}))
    print(output / "decision.json")


if __name__ == "__main__":
    main()
