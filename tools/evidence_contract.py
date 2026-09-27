"""Read-only completion/provenance contract for the formal manuscript evidence.

Call validate_evidence at every build transition and compare its returned
manifest with the preceding transition. Paths in the manifest are relative to
the revision root. No files are written and no solver or plotter is imported.
Completed timeouts may pass; closed optimisation gaps are not required.
"""

from __future__ import annotations

from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
from types import ModuleType


SCOPE = "floating-point tolerance-certified finite path/time model"
PLAN_COUNTS = {
    "core_v1": 96, "policies_v1": 84, "direct_comparison_v1": 48,
    "ablation_v1": 20, "assistance_v1": 60, "groups_v1": 102, "modelling_v1": 44,
}
FIGURE_SECTIONS = {
    "policy_group_service": "5.2_policy_comparisons",
    "resource_sensitivity": "5.3_resource_sensitivity",
    "group_assumptions": "5.4_group_assumptions",
    "modelling_sensitivity": "5.5_modelling_choices",
    "algorithm_comparison": "5.6_computational_performance",
    "anaheim_gap_progress": "5.6_computational_performance",
}
COLLECTION_PATH = "audit/collected_formal_v1/results.json"
COLLECTOR_SHA256 = "ecccdff34c96df137b8140452117854f68f5508f27c404a5c8b1cbbc29e68413"
GENERATOR_SHA256 = "1c6ce4ff47e23ff87261ef34d271ef8e33707e757036f9d1756f1dedbf877824"
PUBLICATION_FIELDS = {"created_utc", "csv_file", "csv_sha256", "csv_columns"}


class EvidenceContractError(ValueError):
    """Evidence is missing, incomplete, changed, or outside the reviewed contract."""


def _require(condition, message):
    if not condition:
        raise EvidenceContractError(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _hash(value, label):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
             f"Invalid SHA-256: {label}")
    return value


def _relative(value, label):
    _require(isinstance(value, str) and bool(value), f"Missing relative path: {label}")
    value = value.replace("\\", "/")
    _require(not PureWindowsPath(value).drive and not value.startswith("/")
             and all(part not in ("", ".", "..") for part in value.split("/"))
             and ":" not in value, f"Unsafe relative path: {label}")
    return value


def _inside(root, path):
    resolved = Path(path).resolve()
    _require(resolved.is_relative_to(root), f"Input leaves revision: {path}")
    return resolved


def _load_collector(root):
    path = _inside(root, root / "research/collect_results.py")
    data = path.read_bytes()
    _require(_sha(data) == COLLECTOR_SHA256, "Collector differs from the audited implementation")
    # Execute only the hash-pinned standard-library collector, without an import
    # loader that might create a __pycache__ file in the research tree.
    module = ModuleType("_evidence_contract_collector")
    module.__file__ = str(path)
    exec(compile(data, str(path), "exec"), module.__dict__)
    return module


class _Snapshot:
    def __init__(self, root, collector):
        self.root = root
        self.reader = collector.Reader()
        self.paths = {}

    def path(self, relative):
        relative = _relative(relative, relative)
        resolved = _inside(self.root, self.root / relative)
        _require(relative not in self.paths or self.paths[relative] == resolved,
                 f"Input path target changed: {relative}")
        self.paths[relative] = resolved
        return resolved

    def data(self, relative):
        return self.reader.data(self.path(relative))

    def document(self, relative):
        return self.reader.json(self.path(relative))

    def checked(self, relative, expected):
        digest = _sha(self.data(relative))
        _require(digest == _hash(expected, relative), f"Changed input: {relative}")
        return digest

    def record(self, relative):
        relative = _relative(relative, relative)
        return {"path": relative, "sha256": _sha(self.data(relative))}

    def check_unchanged(self):
        for relative in tuple(self.paths):
            self.path(relative)
        self.reader.check_unchanged()


def _invariant(snapshot):
    return {key: value for key, value in snapshot.items() if key not in PUBLICATION_FIELDS}


def _csv_bytes(snapshot):
    # This is the audited writer's in-memory encoding, not write_outputs().
    columns = ["collection_status", "collection_is_final"] + list(snapshot["rows"][0])
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in snapshot["rows"]:
        values = {"collection_status": snapshot["status"], "collection_is_final": snapshot["is_final"], **row}
        writer.writerow({key: json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                         if value is None or isinstance(value, (dict, list, bool)) else value
                         for key, value in values.items()})
    return columns, buffer.getvalue().encode("utf-8")


def _controlled_without_incumbent(row):
    stages = row.get("stages")
    return (row.get("plan") == "policies_v1" and row.get("policy") == "controlled"
            and row.get("has_final_result") is True and row.get("progress_completed") is True
            and row.get("failure") is None and row.get("source_certified") is False
            and row.get("validation_passed") is None and row.get("any_timeout") is True
            and row.get("stage_count") == 1 and row.get("incumbent_stage_count") == 0
            and isinstance(stages, list) and len(stages) == 1 and isinstance(stages[0], dict)
            and stages[0].get("status_code") == 108 and stages[0].get("name") == "travel"
            and stages[0].get("callback_error") is None and stages[0].get("certified") is False
            and all(stages[0].get(key) is None for key in ("incumbent", "validation_max"))
            and all(row.get(key) is None for key in (
                "attained_z", "service", "fine_service", "fine_z", "coverage_fraction",
                "travel_person_minutes", "validation_max")))


def _complete_collection(saved):
    _require(type(saved.get("schema_version")) is int and saved["schema_version"] == 1
             and saved.get("scope") == SCOPE and saved.get("freeze") == "formal_v1",
             "Collection schema, precision scope, or freeze mismatch")
    _require(saved.get("status") == "final" and saved.get("is_final") is True
             and saved.get("all_plans_complete") is True and saved.get("allow_incomplete") is False,
             "A strict final collection is required")
    selected = saved.get("selected_plans")
    _require(isinstance(selected, list) and all(isinstance(name, str) for name in selected)
             and len(selected) == len(PLAN_COUNTS) and set(selected) == set(PLAN_COUNTS),
             "Exactly the seven declared formal plans are required")
    _require(type(saved.get("planned_runs")) is int and saved["planned_runs"] == 454,
             "Collection must account for 454 planned runs")
    rows = saved.get("rows")
    _require(isinstance(rows, list) and len(rows) == 454 and all(isinstance(row, dict) for row in rows),
             "Collection must contain 454 planned rows")
    ids = [row.get("run_id") for row in rows]
    _require(all(isinstance(identifier, str) for identifier in ids) and len(set(ids)) == 454,
             "Planned run identifiers must be globally unique")
    _require(Counter(row.get("plan") for row in rows) == Counter(PLAN_COUNTS),
             "Per-plan row counts differ from the declared denominators")
    for row in rows:
        _require(row.get("record_state") == "result" and row.get("has_final_result") is True
                 and row.get("progress_completed") is True and row.get("failure") is None,
                 f"Missing or unacknowledged final result: {row['run_id']}")
        _require(row.get("validation_passed") is True or _controlled_without_incumbent(row),
                 f"Unvalidated final allocation: {row['run_id']}")
    _require(isinstance(saved.get("created_utc"), str) and bool(saved["created_utc"]),
             "Missing collection timestamp")


def _validate(root, evidence_path):
    root = Path(root).resolve(strict=True)
    _require(root.is_dir(), "Revision root must be a directory")
    evidence_path = Path(evidence_path)
    evidence_path = _inside(root, evidence_path if evidence_path.is_absolute() else root / evidence_path)
    evidence_relative = evidence_path.relative_to(root).as_posix()
    collector = _load_collector(root)
    snapshot = _Snapshot(root, collector)
    snapshot.checked("research/collect_results.py", COLLECTOR_SHA256)
    snapshot.checked("research/generate_evidence.py", GENERATOR_SHA256)
    evidence = snapshot.document(evidence_relative)
    _require(type(evidence.get("planned_runs")) is int and evidence["planned_runs"] == 454
             and type(evidence.get("completed_runs")) is int and evidence["completed_runs"] == 454,
             "All 454 declared runs must be complete")
    _require(isinstance(evidence.get("validation"), dict) and evidence["validation"].get("all_passed") is True,
             "Evidence allocations have not all passed validation")
    _require(evidence.get("generator_sha256") == GENERATOR_SHA256, "Evidence generator hash mismatch")
    snapshot.checked(COLLECTION_PATH, evidence.get("collection_sha256"))
    saved = snapshot.document(COLLECTION_PATH)
    _complete_collection(saved)
    _require(saved.get("collector_sha256") == COLLECTOR_SHA256, "Saved collector hash mismatch")

    csv_name = _relative(saved.get("csv_file"), "collection CSV")
    _require("/" not in csv_name, "Collection CSV must be a sibling file")
    csv_relative = (Path(COLLECTION_PATH).parent / csv_name).as_posix()
    snapshot.checked(csv_relative, saved.get("csv_sha256"))
    sources = collector.normalise_hashes(saved.get("source_hashes"), source=True)
    source_hashes = {"research/" + _relative(name, "module"): digest for name, digest in sources.items()}
    for name, digest in source_hashes.items():
        snapshot.checked(name, digest)

    freeze_relative = "research/frozen/formal_v1/manifest.json"
    freeze = snapshot.document(freeze_relative)
    frozen_files = collector.normalise_hashes(freeze.get("files"))
    _require("plans/core_v1.json" in frozen_files, "Missing frozen core-plan pin")
    plan_hashes, planned_ids = {}, {}
    for name, count in PLAN_COUNTS.items():
        relative = "research/plans/" + name + ".json"
        plan = snapshot.document(relative)
        entries = plan.get("runs")
        _require(isinstance(entries, list) and len(entries) == count,
                 f"Wrong declared plan count: {name}")
        ids = [entry["id"] for entry in entries]
        _require(len(set(ids)) == count and not set(ids).intersection(planned_ids),
                 f"Duplicate planned identifiers: {name}")
        planned_ids.update({identifier: name for identifier in ids})
        digest = _sha(snapshot.data(relative))
        pin = frozen_files.get("plans/" + name + ".json")
        _require(pin is None or digest == pin, f"Plan differs from frozen pin: {name}")
        plan_hashes[relative] = digest
    _require({row["run_id"]: row["plan"] for row in saved["rows"]} == planned_ids,
             "Collection rows do not match the actual planned identifiers")

    # Pin the complete observed input set before and after fresh collection, so
    # a change between the two read-only passes cannot silently mix snapshots.
    inputs = saved.get("input_sha256")
    _require(isinstance(inputs, dict) and bool(inputs), "Missing collection input hashes")
    input_hashes = {}
    for name, digest in inputs.items():
        relative = "research/" + _relative(name, "collection input")
        _require(relative not in input_hashes, "Aliased collection input paths")
        input_hashes[relative] = snapshot.checked(relative, digest)
    fresh = collector.collect(root / "research", list(saved["selected_plans"]), allow_incomplete=False)
    _complete_collection(fresh)
    _require(_canonical(_invariant(fresh)) == _canonical(_invariant(saved)),
             "Saved collection differs from a fresh strict snapshot")
    columns, expected_csv = _csv_bytes(fresh)
    _require(saved.get("csv_columns") == columns and snapshot.data(csv_relative) == expected_csv,
             "CSV values or columns differ from the strict snapshot")
    allocations = [row for row in fresh["rows"] if row["validation_passed"] is True]
    unavailable = [row["run_id"] for row in fresh["rows"] if _controlled_without_incumbent(row)]
    _require(evidence["validation"].get("scope") == "all_returned_allocations"
             and evidence["validation"].get("returned_allocations") == len(allocations)
             and evidence["validation"].get("no_incumbent_timeouts") == unavailable,
             "Allocation and no-incumbent counts differ from the collection")
    safeguards = [{"run_id": row["run_id"], "stage": stage["name"],
                   "status_code": stage["status_code"], "callback_error": stage["callback_error"]}
                  for row in fresh["rows"] for stage in row["stages"]
                  if stage.get("callback_error") is not None]
    _require(evidence["validation"].get("numerical_safeguard_terminations", []) == safeguards,
             "Numerical-safeguard terminations differ from the collection")
    maximum = max(row["validation_max"] for row in allocations)
    reported_maximum = collector.number(evidence["validation"].get("maximum_residual"),
                                        "evidence maximum_residual", True)
    _require(reported_maximum == maximum,
             "Evidence validation maximum differs from the collection")

    by_id = {row["run_id"]: row for row in fresh["rows"]}
    figures = evidence.get("figures")
    _require(isinstance(figures, list) and len(figures) == len(FIGURE_SECTIONS)
             and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in figures)
             and {item["name"] for item in figures} == set(FIGURE_SECTIONS),
             "Exactly the six declared figure records are required")
    figure_hashes, figure_input_hashes = {}, {}
    for figure in figures:
        name = figure["name"]
        _require(figure.get("section") == FIGURE_SECTIONS[name], f"Unexpected figure directory: {name}")
        _require(figure.get("collection_sha256") == evidence["collection_sha256"],
                 f"Figure uses another collection: {name}")
        directory = evidence_path.parent / FIGURE_SECTIONS[name]
        provenance = (directory / (name + "_provenance.json")).relative_to(root).as_posix()
        _require(_canonical(snapshot.document(provenance)) == _canonical(figure),
                 f"Figure provenance differs from summary: {name}")
        figure_hashes[provenance] = _sha(snapshot.data(provenance))
        for suffix in ("pdf", "png"):
            relative = (directory / f"{name}.{suffix}").relative_to(root).as_posix()
            figure_hashes[relative] = snapshot.checked(relative, figure.get(suffix + "_sha256"))
        used = figure.get("inputs")
        _require(isinstance(used, list) and bool(used), f"Missing figure inputs: {name}")
        for item in used:
            _require(isinstance(item, dict) and isinstance(item.get("run_id"), str)
                     and item["run_id"] in by_id, f"Unplanned figure input: {name}")
            row = by_id[item["run_id"]]
            _require(item.get("result_sha256") == row["result_sha256"], f"Stale figure input: {name}")
            relative = "research/" + _relative(row["result_path"], "figure result")
            expected = f"research/runs/{row['plan']}/{row['run_id']}/result.json"
            _require(relative == expected, f"Figure result path mismatch: {name}")
            # A result can contribute to several panels in the same figure.
            figure_input_hashes[relative] = snapshot.checked(relative, row["result_sha256"])

    source_hashes.update({"research/collect_results.py": COLLECTOR_SHA256,
                          "research/generate_evidence.py": GENERATOR_SHA256,
                          "research/run_plan.py": snapshot.checked("research/run_plan.py", frozen_files.get("run_plan.py"))})
    manifest = {
        "schema_version": 1, "scope": SCOPE, "freeze": "formal_v1",
        "planned_runs": 454, "completed_runs": 454,
        "evidence": snapshot.record(evidence_relative),
        "collection": {**snapshot.record(COLLECTION_PATH), "csv_path": csv_relative,
                       "csv_sha256": saved["csv_sha256"], "invariant_sha256": _sha(_canonical(_invariant(fresh)))},
        "freeze_manifest": snapshot.record(freeze_relative),
        "source_hashes": dict(sorted(source_hashes.items())),
        "plan_hashes": dict(sorted(plan_hashes.items())),
        "figure_hashes": dict(sorted(figure_hashes.items())),
        "figure_input_hashes": dict(sorted(figure_input_hashes.items())),
        "input_snapshot_sha256": _sha(_canonical(input_hashes)),
    }
    snapshot.check_unchanged()
    return manifest


def validate_evidence(root: Path, evidence_path: Path) -> dict:
    """Validate complete evidence without writing, solving, plotting, or compiling.

    Relative evidence_path values are interpreted against root. Failures raise
    EvidenceContractError. The return value contains hashes, not numerical
    results or a final-delivery approval. No transient timestamp is returned.
    """
    try:
        return _validate(root, evidence_path)
    except EvidenceContractError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, ZeroDivisionError, StopIteration) as exc:
        raise EvidenceContractError(f"Evidence contract rejected: {exc}") from exc
