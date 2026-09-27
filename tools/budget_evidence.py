"""Validate and summarise the fixed paired time-budget follow-up."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import math
from pathlib import Path
import statistics
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
GATE_SHA256 = "ac5d9cebe15fb782040373f7f4e90783f2987c534552f21d0b89c2ed1986797c"
DECISION_PATH = "audit/time_budget_gate_v1/decision.json"
METHODS = {"direct": "Direct MILP", "benders_flow": "Strengthened Benders"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def gate_module(root=ROOT):
    path = root / "tools/assess_time_budget.py"
    source = path.read_bytes()
    require(digest(source) == GATE_SHA256, "Unreviewed time-budget gate")
    module = ModuleType("_budget_evidence_gate")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def number(value, label, nonnegative=True):
    require(type(value) in (int, float) and math.isfinite(value)
            and (not nonnegative or value >= 0), f"Invalid {label}")
    return value


def outcome(row, budget):
    require(row.get("record_state") == "result" and row.get("has_final_result") is True
            and row.get("progress_completed") is True and row.get("failure") is None,
            "Unfinished time-budget outcome")
    require(type(row.get("source_certified")) is bool, "Missing certification status")
    stages = row.get("stages")
    require(isinstance(stages, list) and bool(stages)
            and all(stage.get("callback_error") is None for stage in stages), "Unresolved callback")
    seconds = number(row.get("seconds"), "elapsed time")
    warm = number(row.get("warm_start_seconds"), "warm-start time")
    require(seconds >= warm, "Elapsed time omits warm-start work")
    require(row.get("limit_seconds_per_stage") == budget, "Unexpected stage allowance")
    returned = row.get("validation_passed") is True
    if returned:
        require(row.get("stage_count") == 2 and row.get("incumbent_stage_count") == 2,
                "A returned two-stage outcome lacks a required incumbent")
        for key in ("maxmin_lower_bound", "maxmin_upper_bound", "maxmin_relative_gap",
                    "secondary_lower_bound", "secondary_upper_bound", "secondary_relative_gap"):
            number(row.get(key), key)
        require(row["maxmin_lower_bound"] <= row["maxmin_upper_bound"] + 1e-7,
                "Inverted primary interval")
        require(row["secondary_lower_bound"] <= row["secondary_upper_bound"] + 1e-7,
                "Inverted conditional travel interval")
        require(isinstance(row.get("secondary_targets"), dict)
                and "floor" in row["secondary_targets"], "Missing conditional travel floor")
        number(row["secondary_targets"]["floor"], "transferred floor")
    else:
        require(row.get("validation_passed") is None and row["source_certified"] is False
                and row.get("stage_count") == 1 and row.get("incumbent_stage_count") == 0
                and row.get("any_timeout") is True and stages[0].get("status_code") == 108,
                "Only a completed first-stage no-incumbent timeout may lack an allocation")
        require(all(row.get(key) is None for key in ("maxmin_lower_bound", "maxmin_relative_gap",
                    "secondary_lower_bound", "secondary_upper_bound", "secondary_relative_gap")),
                "No-incumbent outcome contains allocation metrics")
    return {
        "plan": row["plan"], "run_id": row["run_id"], "network": row["settings"]["network"],
        "replication": row["settings"]["replication"], "algorithm": row["algorithm"],
        "budget_seconds_per_stage": budget, "instance_sha256": row["instance_sha256"],
        "result_sha256": row["result_sha256"], "certified": row["source_certified"],
        "returned_allocation": returned, "seconds": seconds, "warm_start_seconds": warm,
        "primary_lower_bound": row.get("maxmin_lower_bound"),
        "primary_upper_bound": row.get("maxmin_upper_bound"),
        "primary_relative_gap": row.get("maxmin_relative_gap"),
        "secondary_lower_bound": row.get("secondary_lower_bound"),
        "secondary_upper_bound": row.get("secondary_upper_bound"),
        "secondary_relative_gap": row.get("secondary_relative_gap"),
        "secondary_targets": row.get("secondary_targets"),
        "stage_statuses": [stage.get("status_code") for stage in stages],
    }


def compare(short, decision, plan=None, longer=None):
    gate = gate_module()
    expected, expected_plan = gate.assess(short)
    require(all(decision.get(key) == value for key, value in expected.items()), "Decision differs from its evidence")
    require(plan == expected_plan, "Follow-up plan differs from the fixed paired rule")
    selected = {item["plan"] + "/" + item["run_id"]: item for item in expected["comparisons"]}
    short_rows = {row["plan"] + "/" + row["run_id"]: row for row in short["rows"]}
    records = [outcome(short_rows[key], 600) for key in selected]
    if expected["triggered"]:
        require(isinstance(longer, dict) and longer.get("status") == "final"
                and longer.get("is_final") is True and longer.get("allow_incomplete") is False
                and longer.get("all_plans_complete") is True and longer.get("freeze") == "formal_v1"
                and longer.get("collector_sha256") == gate.COLLECTOR_SHA256
                and longer.get("selected_plans") == [gate.FOLLOWUP_NAME]
                and longer.get("planned_runs") == 12, "Strict twelve-run follow-up required")
        rows = longer.get("rows", [])
        entries = {entry["id"]: entry for entry in plan["runs"]}
        require(len(rows) == 12 and len({row["run_id"] for row in rows}) == 12
                and {row["run_id"] for row in rows} == set(entries), "Incomplete follow-up family")
        plan_sha = digest(gate.encoded(plan))
        require(decision.get("followup_plan_sha256") == plan_sha, "Follow-up plan hash differs")
        for row in rows:
            entry = entries[row["run_id"]]
            reference = entry["paired_short_run"]
            source = short_rows[reference]
            require(row.get("plan") == gate.FOLLOWUP_NAME and row.get("plan_sha256") == plan_sha
                    and row.get("planned_entry") == entry and row.get("policy") == "proposed"
                    and row.get("algorithm") == entry["algorithm"]
                    and row.get("settings") == source["settings"]
                    and row.get("instance_sha256") == source["instance_sha256"],
                    "Follow-up is not the specified matched experiment")
            records.append(outcome(row, 3600))
    else:
        require(longer is None, "Untriggered follow-up must not supply extra runs")
    counts = Counter((r["network"], r["algorithm"], r["budget_seconds_per_stage"]) for r in records)
    budgets = (600, 3600) if expected["triggered"] else (600,)
    expected_counts = {(n, a, b): 3 for n in ("Anaheim", "Winnipeg") for a in METHODS for b in budgets}
    require(counts == Counter(expected_counts), "Demand-draw denominators changed")
    groups = []
    for network, algorithm, budget in expected_counts:
        cell = [r for r in records if (r["network"], r["algorithm"], r["budget_seconds_per_stage"])
                == (network, algorithm, budget)]
        complete = all(r["returned_allocation"] for r in cell)
        groups.append({
            "network": network, "algorithm": algorithm, "budget_seconds_per_stage": budget,
            "runs": 3, "returned_allocations": sum(r["returned_allocation"] for r in cell),
            "certified": sum(r["certified"] for r in cell),
            "median_seconds": statistics.median(r["seconds"] for r in cell),
            "maximum_primary_relative_gap": max(r["primary_relative_gap"] for r in cell) if complete else None,
            "maximum_secondary_relative_gap": max(r["secondary_relative_gap"] for r in cell) if complete else None,
        })
    return {"schema_version": 1, "scope": "fixed central instances, paired stage-budget comparison",
            "triggered": expected["triggered"], "additional_runs": expected["followup_runs"],
            "total_formal_runs": expected["total_formal_runs"], "groups": groups, "runs": records}


def gap_text(value):
    if value is None:
        return "--"
    percent = 100.0 * number(value, "gap")
    return r"\(<0.001\)" if 0 < percent < 0.001 else f"{percent:.3f}"


def render_table(report):
    rows = [r"\begingroup", r"\small", r"\color{darkred}", r"\setlength{\tabcolsep}{3pt}",
            r"\renewcommand{\arraystretch}{1.08}",
            r"\begin{longtable}{@{}l p{0.23\textwidth} r c r r r@{}}",
            r"\caption{\rev{Paired computational-budget comparison for concentrated demand and moderate scarcity.}}\label{tab:time-budget-comparison}\\",
            r"\toprule Network & Method & \shortstack{Limit\\(s)} & Certified & \shortstack{Median\\(s)} & \shortstack{Primary\\gap (\%)} & \shortstack{Travel\\gap (\%)} \\",
            r"\midrule\endfirsthead", r"\multicolumn{7}{l}{Table~\thetable\ continued}\\",
            r"\toprule Network & Method & \shortstack{Limit\\(s)} & Certified & \shortstack{Median\\(s)} & \shortstack{Primary\\gap (\%)} & \shortstack{Travel\\gap (\%)} \\",
            r"\midrule\endhead", r"\bottomrule\endfoot"]
    for cell in report["groups"]:
        rows.append(" & ".join((cell["network"], METHODS[cell["algorithm"]], str(cell["budget_seconds_per_stage"]),
                               f"{cell['certified']}/3", f"{cell['median_seconds']:.1f}",
                               gap_text(cell["maximum_primary_relative_gap"]),
                               gap_text(cell["maximum_secondary_relative_gap"]))) + r" \\")
    missing = (r" A dash denotes a cell with at least one run without an allocation, retained in the denominator."
               if any(cell["returned_allocations"] < cell["runs"] for cell in report["groups"]) else "")
    rows.extend([r"\end{longtable}", r"\noindent\footnotesize\rev{Limits apply to each MIP stage. "
                 r"Medians include both stages and warm-start work, including time-limited outcomes. "
                 r"Gaps are the largest across the three paired demand draws. Primary gaps use the final plan's "
                 r"service floor. Travel gaps are conditional on each run's transferred floor." + missing + "}",
                 r"\endgroup", ""])
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(ROOT / "audit") and not output.exists(), "Use a new audit directory")
    gate = gate_module()
    collector = gate.load_collector(ROOT)
    reader = collector.Reader()
    decision_file = ROOT / DECISION_PATH
    decision = reader.json(decision_file)
    require(digest(reader.data(ROOT / "audit/time_budget_review.md")) == gate.RULE_SHA256,
            "Changed time-budget rule")
    require(decision.get("tool_sha256") == GATE_SHA256, "Decision used another gate")
    freeze_file = ROOT / "research/frozen/formal_v1/manifest.json"
    require(digest(reader.data(freeze_file)) == decision.get("freeze_manifest_sha256"), "Freeze changed")
    short_path = (ROOT / decision["short_collection"]).resolve()
    require(short_path.is_relative_to(ROOT / "audit"), "Unsafe short collection path")
    require(digest(reader.data(short_path)) == decision["short_collection_sha256"], "Short evidence changed")
    short = reader.json(short_path)
    fresh = collector.collect(ROOT / "research", list(gate.PLANS), allow_incomplete=False)
    excluded = {"created_utc", "csv_file", "csv_sha256", "csv_columns"}
    require({k: v for k, v in short.items() if k not in excluded}
            == {k: v for k, v in fresh.items() if k not in excluded}, "Short evidence is stale")
    plan, longer = None, None
    if decision["triggered"]:
        plan_path = (ROOT / decision["followup_plan"]).resolve()
        require(plan_path == ROOT / "research/plans" / (gate.FOLLOWUP_NAME + ".json"), "Unexpected plan path")
        require(digest(reader.data(plan_path)) == decision["followup_plan_sha256"], "Follow-up plan changed")
        plan = reader.json(plan_path)
        longer = collector.collect(ROOT / "research", [gate.FOLLOWUP_NAME], allow_incomplete=False)
    report = compare(short, decision, plan, longer)
    report.update({"decision_path": DECISION_PATH, "decision_sha256": digest(reader.data(decision_file)),
                   "tool_sha256": digest(Path(__file__).read_bytes()), "gate_sha256": GATE_SHA256,
                   "short_collection_sha256": digest(reader.data(short_path))})
    reader.check_unchanged()
    output.mkdir(parents=True, exist_ok=False)
    if longer is not None:
        collector.write_outputs(longer, output / "long_budget", ROOT / "research")
        report["long_collection_sha256"] = digest((output / "long_budget/results.json").read_bytes())
    table = render_table(report).encode("utf-8")
    gate.write_new(output / "budget_summary.tex", table)
    report["table_sha256"] = digest(table)
    gate.write_new(output / "budget_comparison.json", gate.encoded(report))
    print(output / "budget_comparison.json")


if __name__ == "__main__":
    main()
