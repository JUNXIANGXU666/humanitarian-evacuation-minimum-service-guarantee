"""Require the original evidence and the fixed time-budget decision together."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import evidence_contract as baseline


BUDGET_SHA256 = "b10430c5e88d2a09cb6b8b8d48bc95ab797158bc635d3252d20b2fb2bf5ea84a"
REPORT_PATH = "audit/time_budget_summary_v1/budget_comparison.json"
SHORT_PATH = "audit/time_budget_gate_v1/short_budget/results.json"
LONG_PATH = "audit/time_budget_summary_v1/long_budget/results.json"
TABLE_PATH = "audit/time_budget_summary_v1/budget_summary.tex"


def _reporter(root, snapshot):
    relative = "tools/budget_evidence.py"
    snapshot.checked(relative, BUDGET_SHA256)
    path = root / relative
    module = ModuleType("_complete_budget_reporter")
    module.__file__ = str(path)
    exec(compile(snapshot.data(relative), str(path), "exec"), module.__dict__)
    return module


def _collection(root, snapshot, relative, collector, selected):
    saved = snapshot.document(relative)
    inputs = saved.get("input_sha256")
    baseline._require(isinstance(inputs, dict) and bool(inputs), "Missing budget collection inputs")
    for name, digest in inputs.items():
        snapshot.checked("research/" + baseline._relative(name, "budget collection input"), digest)
    fresh = collector.collect(root / "research", selected, allow_incomplete=False)
    baseline._require(baseline._invariant(saved) == baseline._invariant(fresh),
                      "Budget collection differs from its current strict snapshot")
    csv_name = baseline._relative(saved.get("csv_file"), "budget CSV")
    baseline._require("/" not in csv_name, "Budget CSV must be a sibling file")
    csv_path = (Path(relative).parent / csv_name).as_posix()
    snapshot.checked(csv_path, saved.get("csv_sha256"))
    columns, content = baseline._csv_bytes(fresh)
    baseline._require(saved.get("csv_columns") == columns and snapshot.data(csv_path) == content,
                      "Budget CSV differs from the strict snapshot")
    return saved


def validate_budget(root):
    """Return a read-only budget manifest, including a mandatory no-follow-up decision."""
    root = Path(root).resolve(strict=True)
    collector = baseline._load_collector(root)
    snapshot = baseline._Snapshot(root, collector)
    snapshot.checked("research/collect_results.py", baseline.COLLECTOR_SHA256)
    reporter = _reporter(root, snapshot)
    snapshot.checked("tools/assess_time_budget.py", reporter.GATE_SHA256)
    gate = reporter.gate_module(root)
    snapshot.checked("audit/time_budget_review.md", gate.RULE_SHA256)
    decision = snapshot.document(reporter.DECISION_PATH)
    baseline._require(decision.get("tool_sha256") == reporter.GATE_SHA256
                      and decision.get("solver_started") is False
                      and isinstance(decision.get("created_utc"), str)
                      and bool(decision["created_utc"]), "Invalid recorded budget decision")
    snapshot.checked("research/frozen/formal_v1/manifest.json", decision.get("freeze_manifest_sha256"))
    baseline._require(decision.get("short_collection") == SHORT_PATH, "Unexpected short-budget collection")
    snapshot.checked(SHORT_PATH, decision.get("short_collection_sha256"))
    short = _collection(root, snapshot, SHORT_PATH, collector, list(gate.PLANS))
    baseline._require(decision.get("source_hashes") == short.get("source_hashes"),
                      "Budget decision source identities differ")
    plan, longer = None, None
    plan_path = "research/plans/" + gate.FOLLOWUP_NAME + ".json"
    if decision.get("triggered") is True:
        baseline._require(decision.get("followup_plan") == plan_path, "Unexpected follow-up plan path")
        snapshot.checked(plan_path, decision.get("followup_plan_sha256"))
        plan = snapshot.document(plan_path)
        longer = _collection(root, snapshot, LONG_PATH, collector, [gate.FOLLOWUP_NAME])
    else:
        baseline._require(decision.get("triggered") is False
                          and "followup_plan" not in decision and "followup_plan_sha256" not in decision
                          and not (root / plan_path).exists()
                          and not (root / "research/runs" / gate.FOLLOWUP_NAME).exists()
                          and not (root / LONG_PATH).exists(), "Untriggered follow-up contains extra evidence")
    expected = reporter.compare(short, decision, plan, longer)
    expected.update({"decision_path": reporter.DECISION_PATH,
                     "decision_sha256": baseline._sha(snapshot.data(reporter.DECISION_PATH)),
                     "tool_sha256": BUDGET_SHA256, "gate_sha256": reporter.GATE_SHA256,
                     "short_collection_sha256": baseline._sha(snapshot.data(SHORT_PATH))})
    if longer is not None:
        expected["long_collection_sha256"] = baseline._sha(snapshot.data(LONG_PATH))
    table = reporter.render_table(expected).encode("utf-8")
    expected["table_sha256"] = baseline._sha(table)
    baseline._require(snapshot.document(REPORT_PATH) == expected,
                      "Budget report differs from the fixed paired calculation")
    baseline._require(snapshot.data(TABLE_PATH) == table, "Budget table differs from the paired outcomes")
    records = {relative: baseline._sha(snapshot.data(relative)) for relative in tuple(snapshot.paths)}
    snapshot.check_unchanged()
    return {"schema_version": 1, "triggered": expected["triggered"],
            "additional_runs": expected["additional_runs"], "total_formal_runs": expected["total_formal_runs"],
            "report": {"path": REPORT_PATH, "sha256": records[REPORT_PATH]},
            "table": {"path": TABLE_PATH, "sha256": records[TABLE_PATH]},
            "input_hashes": dict(sorted(records.items()))}


def validate_complete_evidence(root, evidence_path):
    """Validate every required run without pooling different stage allowances."""
    try:
        original = baseline.validate_evidence(root, evidence_path)
        budget = validate_budget(root)
        baseline._require(original["planned_runs"] == 454 and original["completed_runs"] == 454,
                          "Original experiment denominator changed")
        for relative, digest in budget["input_hashes"].items():
            path = baseline._inside(Path(root).resolve(), Path(root) / relative)
            baseline._require(baseline._sha(path.read_bytes()) == digest,
                              f"Budget evidence changed during verification: {relative}")
        baseline._require(baseline.validate_evidence(root, evidence_path) == original,
                          "Original evidence changed during budget verification")
        return {**original, "baseline_planned_runs": 454, "baseline_completed_runs": 454,
                "planned_runs": budget["total_formal_runs"], "completed_runs": budget["total_formal_runs"],
                "budget_evidence": budget,
                "completion_verifier_sha256": baseline._sha(Path(__file__).read_bytes())}
    except baseline.EvidenceContractError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, ZeroDivisionError, StopIteration) as exc:
        raise baseline.EvidenceContractError(f"Complete evidence rejected: {exc}") from exc
