"""Collect named frozen plans without importing or running an optimisation solver."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone


SCOPE = "floating-point tolerance-certified finite path/time model"
ALGORITHMS = {"direct", "benders_basic", "benders_bounds", "benders_fractional",
              "benders_warm", "benders_flow"}
POLICIES = {"proposed", "aggregate", "aggregate_fair", "controlled", "proportional",
            "weighted", "coverage", "leximin"}
RAW_FILES = {"ND": ["ND.json"], "SF": ["SiouxFalls_net.tntp", "SiouxFalls_trips.tntp"],
             "Anaheim": ["Anaheim_net.tntp", "Anaheim_trips.tntp"],
             "Winnipeg": ["Winnipeg_net.tntp", "Winnipeg_trips.tntp"]}
CAVEATS = [
    "Bounds retain the frozen implementation's floating-point tolerance scope, not rational or interval certification.",
    "Later-stage bounds are conditional on the recorded preserved targets, not exact lexicographic certificates.",
    "Attained z for aggregate, weighted, aggregate_fair, controlled and proportional policies is not reported as an unrestricted max-min objective interval.",
    "LP solve counts are not logged by the frozen implementation. Callback counts include cache hits and are not LP solve counts.",
    "Final means complete accounting for the named plans, not that every run succeeded or closed its optimality gap.",
    "This collector checks saved metric identities and provenance. It does not regenerate instances, rerun feasibility checks or solve any model.",
]
METRIC_FIELDS = (
    "attained_z service fine_service fine_z fine_group_labels fairness_groups coverage_fraction served_people unmet_fraction "
    "travel_normalised_minutes travel_person_minutes travel_person_hours total_demand "
    "primary_name primary_sense primary_scope primary_units primary_lower_bound "
    "primary_upper_bound primary_absolute_gap primary_relative_gap maxmin_lower_bound "
    "maxmin_upper_bound maxmin_absolute_gap maxmin_relative_gap secondary_name "
    "secondary_scope secondary_units secondary_lower_bound secondary_upper_bound "
    "secondary_absolute_gap secondary_relative_gap secondary_targets "
    "secondary_lower_person_minutes secondary_upper_person_minutes "
    "final_preserved_policy_objectives objective_intervals stages "
    "seconds warm_start_seconds nodes feasibility_cuts optimality_cuts integer_calls "
    "fractional_calls lp_seconds recourse_lp_solves validation_max physical_validation_max policy_validation_max "
    "validation_passed any_timeout source_certified stage_count incumbent_stage_count "
    "transferred_floor controlled_target dimensions solver threads mip_gap"
).split()


class CollectionError(ValueError):
    pass


class SnapshotChanged(CollectionError):
    pass


def require(condition, message):
    if not condition:
        raise CollectionError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
            and value not in (".", ".."), f"Unsafe plan or run name: {value!r}")
    return value


def normalise_hashes(mapping, source=False):
    require(isinstance(mapping, dict) and bool(mapping), "Missing hash map")
    result = {}
    for key, value in mapping.items():
        key = key.replace("\\", "/")
        if source and key.startswith("research/"):
            key = key[len("research/"):]
        require(key not in result and isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value), f"Invalid hash entry: {key}")
        require(not key.startswith("/") and ".." not in key.split("/"), f"Unsafe hash path: {key}")
        result[key] = value
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise CollectionError(f"Non-finite JSON number: {value}")


def _finite_tree(value):
    if isinstance(value, float):
        require(math.isfinite(value), "Non-finite JSON number")
    elif isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def read_bytes(path, optional=False):
    """Read one stable file version, including during the runner's atomic replace."""
    path = Path(path)
    for attempt in range(5):
        try:
            before = path.stat()
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                data = stream.read()
                ended = os.fstat(stream.fileno())
            after = path.stat()
            if (_signature(before) == _signature(opened) == _signature(ended) == _signature(after)
                    and len(data) == after.st_size):
                return data
        except FileNotFoundError:
            if optional and not path.exists():
                return None
        except PermissionError:
            pass
        if attempt < 4:
            time.sleep(0.02 * (attempt + 1))
    raise SnapshotChanged(f"File unavailable or changing during read: {path}")


class Reader:
    def __init__(self):
        self.hashes = {}

    def data(self, path, optional=False):
        path = Path(path).resolve()
        data = read_bytes(path, optional)
        digest = None if data is None else sha256(data)
        if path in self.hashes and self.hashes[path] != digest:
            raise SnapshotChanged(f"Input changed within snapshot: {path}")
        self.hashes[path] = digest
        return data

    def json(self, path, optional=False):
        data = self.data(path, optional)
        if data is None:
            return None
        try:
            value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs,
                               parse_constant=_reject_constant)
            _finite_tree(value)
            require(isinstance(value, dict), f"Expected a JSON object in {path}")
            return value
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CollectionError(f"Invalid JSON in {path}: {exc}") from exc

    def check_unchanged(self):
        for path, expected in self.hashes.items():
            data = read_bytes(path, optional=expected is None)
            if (None if data is None else sha256(data)) != expected:
                raise SnapshotChanged(f"Input changed before snapshot completion: {path}")


def number(value, label, nonnegative=False):
    require(type(value) in (int, float) and math.isfinite(value), f"Invalid number: {label}")
    require(not nonnegative or value >= 0, f"Negative value: {label}")
    return value


def close(actual, expected, label):
    number(actual, label)
    number(expected, label)
    require(math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-9),
            f"Inconsistent {label}: {actual!r} != {expected!r}")


def checked_gap(value, bound, maximise, record=None):
    if value is None or bound is None:
        return None, None, None
    number(value, "objective")
    number(bound, "bound")
    inversion = max(0.0, value - bound if maximise else bound - value)
    require(inversion <= 1e-7 * max(1.0, abs(value)), "Material objective/bound inversion")
    gap = max(0.0, bound - value if maximise else value - bound)
    relative = gap / max(1e-10, abs(value))
    if record is not None:
        close(record.get("absolute_gap"), gap, "absolute gap")
        close(record.get("relative_gap"), relative, "relative gap")
    return gap, relative, inversion


def gap_closed(record):
    return (record.get("absolute_gap") is not None and record.get("relative_gap") is not None
            and (record["absolute_gap"] <= 1.00001e-9 or record["relative_gap"] <= 1.00001e-4))


def policy_value(metrics, name):
    if name == "floor":
        return metrics["z"]
    if name in ("aggregate", "aggregate_fair", "coverage"):
        return metrics["served"]
    if name == "weighted":
        return math.fsum((g + 1) * amount for row in metrics["served_by_origin_group"]
                         for g, amount in enumerate(row))
    if name.startswith("ordered_sum_"):
        return math.fsum(sorted(metrics["service"])[:int(name.rsplit("_", 1)[1])])
    require(name == "travel", f"Unknown objective: {name}")
    return metrics["travel_normalised_minutes"] / 60.0


def expected_stages(policy, instance):
    if policy in ("proposed", "proportional"):
        return ["floor", "travel"]
    if policy == "controlled":
        return ["travel"]
    if policy == "aggregate_fair":
        return ["aggregate_fair", "floor", "travel"]
    if policy in ("aggregate", "weighted"):
        return [policy, "travel"]
    if policy == "coverage":
        return ["floor", "coverage", "travel"]
    return ["floor"] + [f"ordered_sum_{k}" for k in range(2, len(instance["fairness_groups"]) + 1)] + ["travel"]


def objective_scope(policy, name, targets):
    if policy == "controlled":
        return "matched_coverage_policy"
    if policy == "proportional":
        return "proportional_allocation_policy" + ("_conditional_on_targets" if targets else "")
    if targets:
        return "conditional_on_preserved_targets"
    if name == "floor":
        return "unrestricted_maxmin_within_declared_finite_model"
    return "policy_" + name


def units(name):
    if name == "travel":
        return "normalised_person_hours"
    if name == "weighted":
        return "weighted_normalised_population"
    if name == "floor" or name.startswith("ordered_sum_"):
        return "service_ratio" if name == "floor" else "sum_of_service_ratios"
    return "normalised_population"


def interval(name, maximise, value, bound, targets, policy, usable=True, record=None):
    gap, relative, inversion = checked_gap(value, bound, maximise, record)
    lower, upper = (value, bound) if maximise else (bound, value)
    # Outward widening prevents a tiny tolerated inversion becoming a crossed interval.
    if inversion:
        lower, upper = value, value
    return {"name": name, "sense": "max" if maximise else "min", "units": units(name),
            "scope": objective_scope(policy, name, targets), "targets": targets,
            "achieved": value, "reported_solver_bound": bound,
            "lower_bound": lower if usable else None, "upper_bound": upper if usable else None,
            "absolute_gap": gap if usable else None, "relative_gap": relative if usable else None,
            "bound_inversion": inversion, "bounds_widened_for_roundoff": bool(inversion),
            "bound_usable": usable}


def metrics_check(metrics, stage, instance):
    """Check reported physical metrics against the saved assignment, not a solver."""
    require(isinstance(metrics, dict), "Missing stage validation")
    violations = metrics.get("violations")
    require(isinstance(violations, dict) and bool(violations), "Missing feasibility residuals")
    for name, value in violations.items():
        number(value, "validation." + name, True)
    maximum = max(violations.values())
    tolerance = number(metrics.get("normalised_tolerance"), "normalised_tolerance", True)
    require(tolerance == 1e-7 and metrics.get("passed") is (maximum <= tolerance),
            "Inconsistent validation flag or tolerance")
    x = stage.get("solution", {}).get("x")
    no, ng, nt = len(instance["origins"]), len(instance["groups"]), instance["departures"]
    require(isinstance(x, list) and len(x) == len(instance["paths"]) * ng * nt,
            "Saved assignment dimension mismatch")
    assigned = [[0.0] * ng for _ in range(no)]
    travel = []
    for p, path in enumerate(instance["paths"]):
        for g in range(ng):
            amount = math.fsum(x[(p * ng + g) * nt:(p * ng + g + 1) * nt])
            assigned[path["origin"]][g] += amount
            travel.append(path["time"] * amount)
    demand = instance["demand"]
    fine = [math.fsum(row[g] for row in assigned) / math.fsum(row[g] for row in demand) for g in range(ng)]
    service = [math.fsum(row[g] for row in assigned for g in group) /
               math.fsum(row[g] for row in demand for g in group) for group in instance["fairness_groups"]]
    require(len(metrics["fine_service"]) == ng and len(metrics["service"]) == len(service),
            "Service vector dimension mismatch")
    for actual, expected in zip(metrics["fine_service"] + metrics["service"], fine + service):
        close(actual, expected, "service")
    require(len(metrics["served_by_origin_group"]) == no, "Origin metric dimension mismatch")
    for actual, expected in zip(metrics["served_by_origin_group"], assigned):
        require(len(actual) == ng, "Group metric dimension mismatch")
        for a, b in zip(actual, expected):
            close(a, b, "served_by_origin_group")
    served = math.fsum(amount for row in assigned for amount in row)
    close(metrics["z"], min(service), "z")
    close(metrics["served"], served, "coverage")
    close(metrics["unmet"], 1.0 - served, "unmet")
    close(metrics["travel_normalised_minutes"], math.fsum(travel), "normalised travel")
    close(metrics["travel_person_minutes"], math.fsum(travel) * instance["total_demand"], "physical travel")
    return maximum


def extract_result(result, instance, entry):
    """Normalise the two frozen result schemas; do not promote policy z to max-min bounds."""
    fields = dict.fromkeys(METRIC_FIELDS)
    policy = entry.get("policy", "proposed")
    require(result.get("algorithm") == entry["algorithm"], "Result algorithm mismatch")
    require(result.get("policy", "proposed") == policy, "Result policy mismatch")
    if policy == "proposed":
        require("stages" not in result, "Unexpected policy stage schema")
        require(result.get("normalised_flows") is True, "Unexpected flow units")
        stages = [result.get("stage1")] + ([result["stage2"]] if result.get("stage2") is not None else [])
    else:
        require("stage1" not in result, "Unexpected main stage schema")
        stages = result.get("stages")
    expected = expected_stages(policy, instance)
    require(isinstance(stages, list) and 0 < len(stages) <= len(expected), "Invalid stage count")
    require(type(result.get("certified")) is bool, "Missing result certification flag")
    protected, stage_summaries, all_intervals = {}, [], []
    residuals, policy_residuals, viable = [], [], []
    for index, stage in enumerate(stages):
        require(isinstance(stage, dict), "Missing stage record")
        name, maximise = expected[index], expected[index] != "travel"
        require(stage.get("limit_seconds") == entry["seconds"], "Stage time limit mismatch")
        if policy == "proposed":
            require(stage.get("stage") == index + 1 and stage.get("algorithm") == entry["algorithm"],
                    "Stage index/algorithm mismatch")
            require("callback_error" in stage, "Missing callback error status")
            targets = {} if index == 0 else {"floor": result.get("transferred_floor")}
        else:
            require(stage.get("name") == name and stage.get("maximise") is maximise, "Policy stage order/sense mismatch")
            require(stage.get("protected_targets") == protected, "Policy stage targets mismatch")
            targets = dict(protected)
        for field in ("seconds", "nodes"):
            number(stage.get(field), "stage." + field, True)
        require(type(stage.get("certified")) is bool and isinstance(stage.get("status"), str),
                "Missing stage status/certification")
        value, bound = stage.get("incumbent"), stage.get("bound")
        if bound is not None:
            number(bound, "stage bound")
        validation = stage.get("validation")
        usable = stage.get("callback_error") is None
        if value is not None:
            residuals.append(metrics_check(validation, stage, instance))
            close(value, policy_value(validation, name), "stage objective")
            checked_gap(value, bound, maximise, stage)
            require(bound is not None, "Incumbent without recorded objective bound")
            usable = usable and validation["passed"]
            viable.append(stage)
            if policy != "proposed":
                check = stage.get("policy_validation")
                require(isinstance(check, dict) and isinstance(check.get("violations"), dict),
                        "Missing policy validation")
                for residual in check["violations"].values():
                    number(residual, "policy residual", True)
                pmax = max(check["violations"].values(), default=0.0)
                require(check.get("passed") is (pmax <= 1e-7), "Inconsistent policy validation flag")
                policy_residuals.append(pmax)
                usable = usable and check["passed"]
            require(not stage["certified"] or (usable and gap_closed(stage)), "Unsupported stage certification flag")
        else:
            require(validation is None and not stage["certified"] and index == len(stages) - 1,
                    "Invalid no-incumbent stage")
        objective = interval(name, maximise, value, bound, targets, policy, usable,
                             stage if value is not None else None)
        all_intervals.append(objective)
        summary = {key: stage.get(key) for key in ("seconds", "limit_seconds", "status", "status_code", "nodes",
                   "certified", "incumbent", "bound", "absolute_gap", "relative_gap", "solver_relative_gap", "callback_error")}
        summary.update({"name": name, "targets": targets, "cut_stats": stage.get("cut_stats"),
                        "validation_max": max(validation["violations"].values()) if validation else None,
                        "policy_validation": stage.get("policy_validation"), "interval": objective})
        stage_summaries.append(summary)
        if value is not None and maximise:
            protected[name] = max(0.0, value - 1e-8)
    if stages[-1].get("incumbent") is not None:
        require(len(stages) == len(expected), "Final result truncated after a feasible stage")
    metrics = result.get("metrics")
    latest = viable[-1]["validation"] if viable else None
    require(metrics == latest, "Final metrics do not match last available validated stage")
    if policy == "proposed" and len(stages) == 2:
        close(result.get("transferred_floor"), max(0.0, stages[0]["validation"]["z"] - 1e-8), "transferred floor")
    if metrics is not None:
        fields.update({"attained_z": metrics["z"], "service": metrics["service"],
                       "fine_service": metrics["fine_service"], "fine_z": min(metrics["fine_service"]),
                       "coverage_fraction": metrics["served"], "served_people": metrics["served"] * instance["total_demand"],
                       "unmet_fraction": metrics["unmet"], "travel_normalised_minutes": metrics["travel_normalised_minutes"],
                       "travel_person_minutes": metrics["travel_person_minutes"],
                       "travel_person_hours": metrics["travel_person_minutes"] / 60.0})
        if policy == "proposed":
            final = result.get("final_primary")
            require(isinstance(final, dict), "Missing final primary gap")
            close(final.get("incumbent"), metrics["z"], "final primary incumbent")
            require(final.get("bound") == stages[0]["bound"], "Final primary bound mismatch")
            all_intervals[0] = interval("floor", True, metrics["z"], final["bound"], {}, policy,
                                        all_intervals[0]["bound_usable"] and metrics["passed"], final)
        else:
            final = result.get("final_objectives")
            require(isinstance(final, list) and len(final) == len(viable), "Missing final policy objectives")
            fields["final_preserved_policy_objectives"] = final
            for index, record in enumerate(final):
                name = expected[index]
                require(record.get("name") == name and record.get("conditional_bound") == stages[index]["bound"],
                        "Final policy objective/bound mismatch")
                close(record.get("achieved"), policy_value(metrics, name), "final policy objective")
                require(record.get("preserved_target") == protected.get(name), "Final preserved target mismatch")
                close(record.get("target_shortfall"), max(0.0, protected.get(name, record["achieved"]) - record["achieved"]),
                      "final target shortfall")
                all_intervals[index] = interval(name, name != "travel", record["achieved"], record["conditional_bound"],
                                               all_intervals[index]["targets"], policy,
                                               all_intervals[index]["bound_usable"] and metrics["passed"], record)
    elif policy != "proposed":
        require(result.get("final_objectives") == [], "Policy objectives without incumbent")
    warm_seconds = number(result.get("warm_start_seconds", 0.0), "warm_start_seconds", True)
    total = math.fsum(stage["seconds"] for stage in stages) + warm_seconds
    close(result.get("total_seconds"), total, "total seconds")
    fields.update({"total_demand": instance["total_demand"], "fine_group_labels": instance["groups"],
                   "fairness_groups": instance["fairness_groups"], "seconds": result["total_seconds"],
                   "warm_start_seconds": result.get("warm_start_seconds"), "nodes": sum(s["nodes"] for s in stages),
                   "stage_count": len(stages), "incumbent_stage_count": len(viable),
                   "validation_max": max(residuals + policy_residuals, default=None),
                   "physical_validation_max": max(residuals, default=None),
                   "policy_validation_max": max(policy_residuals, default=None),
                   "validation_passed": None if latest is None else bool(latest["passed"] and max(policy_residuals, default=0.0) <= 1e-7),
                   "any_timeout": any("time limit" in s["status"].lower() for s in stages),
                   "source_certified": result["certified"], "stages": stage_summaries, "objective_intervals": all_intervals})
    for key in ("transferred_floor", "controlled_target", "dimensions", "solver", "threads", "mip_gap"):
        fields[key] = result.get(key)
    for key in ("feasibility_cuts", "optimality_cuts", "integer_calls", "fractional_calls", "lp_seconds"):
        values = [s.get("cut_stats", {}).get(key) for s in stages]
        for value in values:
            if value is not None:
                number(value, key, True)
        fields[key] = sum(values) if all(v is not None for v in values) else None
    primary = all_intervals[0]
    for key in ("name", "sense", "scope", "units", "lower_bound", "upper_bound", "absolute_gap", "relative_gap"):
        fields["primary_" + key] = primary[key]
    if primary["scope"] == "unrestricted_maxmin_within_declared_finite_model":
        for key in ("lower_bound", "upper_bound", "absolute_gap", "relative_gap"):
            fields["maxmin_" + key] = primary[key]
    secondary = next((s for s in all_intervals[1:] if s["name"] == "travel"), None)
    if secondary:
        for key in ("name", "scope", "units", "lower_bound", "upper_bound", "absolute_gap", "relative_gap", "targets"):
            fields["secondary_" + key] = secondary[key]
        for direction in ("lower", "upper"):
            bound = secondary[direction + "_bound"]
            fields["secondary_" + direction + "_person_minutes"] = None if bound is None else bound * 60.0 * instance["total_demand"]
    require(not result["certified"] or (len(stages) == len(expected) and all(s["certified"] for s in stages)
            and all(gap_closed(s) and s["bound_usable"] for s in all_intervals)), "Unsupported final certification flag")
    return fields


class Collector:
    def __init__(self, root, reader=None):
        self.root = Path(root).resolve()
        self.reader = reader or Reader()
        self.manifest_path = self.root / "frozen" / "formal_v1" / "manifest.json"
        self.manifest = self.reader.json(self.manifest_path)
        require(self.manifest.get("version") == "formal_v1" and self.manifest["gate"]["verdict"] == "pass"
                and self.manifest["gate"]["scope"] == SCOPE, "Missing reviewed formal_v1 gate")
        self.sources = normalise_hashes(self.manifest["source_hashes"], source=True)
        require(normalise_hashes(self.manifest["gate"]["source_hashes"], source=True) == self.sources,
                "Gate and freeze source hash mismatch")
        current = {p.relative_to(self.root).as_posix(): sha256(self.reader.data(p))
                   for p in sorted((self.root / "evacuation").glob("*.py"))}
        require(current == self.sources, "Current modules differ from reviewed freeze")
        self.frozen_files = normalise_hashes(self.manifest["files"])
        require(self.frozen_files.get("run_plan.py") == sha256(self.reader.data(self.root / "run_plan.py")),
                "Runner differs from reviewed freeze")
        tree = ast.parse(self.reader.data(self.root / "evacuation" / "data.py").decode("utf-8-sig"))
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
        fields = [node for node in settings.body if isinstance(node, ast.AnnAssign)]
        self.defaults = {node.target.id: ast.literal_eval(node.value) for node in fields if node.value is not None}
        self.required = {node.target.id for node in fields if node.value is None}
        self.setting_names = [node.target.id for node in fields]
        self.plan_cache = {}

    def plan(self, name):
        safe_name(name)
        if name in self.plan_cache:
            return self.plan_cache[name]
        path = self.root / "plans" / (name + ".json")
        plan = self.reader.json(path)
        digest = self.reader.hashes[path.resolve()]
        require(isinstance(plan.get("purpose"), str) and isinstance(plan.get("runs"), list)
                and len(plan["runs"]) > 0, f"Invalid plan {name}")
        require(plan.get("planned_runs", len(plan["runs"])) == len(plan["runs"]), f"Plan count mismatch: {name}")
        pinned = self.frozen_files.get("plans/" + name + ".json")
        require(pinned is None or pinned == digest, f"Plan differs from freeze: {name}")
        ids = set()
        for entry in plan["runs"]:
            run_id = safe_name(entry["id"])
            require(run_id not in ids, f"Duplicate planned run: {name}/{run_id}")
            ids.add(run_id)
            require(entry.get("algorithm") in ALGORITHMS and entry.get("policy", "proposed") in POLICIES,
                    f"Unknown algorithm/policy in {name}/{run_id}")
            require(entry.get("policy", "proposed") == "proposed" or entry["algorithm"] == "direct",
                    "Policy plan must specify direct algorithm")
            number(entry.get("seconds"), "planned seconds", True)
            require(entry["seconds"] > 0, "Nonpositive stage limit")
            self.settings(entry)
        self.plan_cache[name] = plan, digest
        return plan, digest

    def settings(self, entry):
        given = entry.get("settings")
        require(isinstance(given, dict) and self.required <= given.keys()
                and given.keys() <= set(self.setting_names), "Invalid planned settings")
        settings = {**self.defaults, **given}
        require(settings["network"] in RAW_FILES, "Unknown formal network")
        return settings

    def provenance(self, provenance, plan, digest, entry):
        require(provenance.get("plan_sha256") == digest, "Provenance plan hash mismatch")
        require(normalise_hashes(provenance.get("source_hashes"), source=True) == self.sources,
                "Provenance module hash mismatch")
        require(json.dumps(provenance.get("entry"), sort_keys=True) == json.dumps(entry, sort_keys=True),
                "Provenance plan entry mismatch")
        require(provenance.get("purpose") == plan["purpose"], "Provenance purpose mismatch")

    def load_result(self, plan_name, plan, digest, entry, check_reference=True):
        directory = self.root / "runs" / plan_name / entry["id"]
        result = self.reader.json(directory / "result.json", optional=True)
        if result is None:
            return None, None
        provenance = self.reader.json(directory / "provenance.json")
        self.provenance(provenance, plan, digest, entry)
        instance = self.reader.json(directory / "instance.json")
        settings = self.settings(entry)
        require(instance.get("settings") == settings and result.get("settings") == settings,
                "Result/instance settings differ from plan")
        instance_digest = sha256(json.dumps(instance, sort_keys=True).encode())
        require(result.get("instance_sha256") == instance_digest, "Result/instance digest mismatch")
        raw_hashes = normalise_hashes(instance.get("source_hashes"))
        require(set(raw_hashes) == set(RAW_FILES[settings["network"]]), "Instance raw input set mismatch")
        for filename, expected in raw_hashes.items():
            require(self.frozen_files.get("data/raw/" + filename) == expected
                    and sha256(self.reader.data(self.root / "data" / "raw" / filename)) == expected,
                    "Raw input hash mismatch: " + filename)
        number(instance.get("total_demand"), "total_demand", True)
        require(instance["total_demand"] > 0, "Empty physical demand")
        close(math.fsum(v for row in instance["demand"] for v in row), 1.0, "normalised input demand")
        if entry.get("policy") == "controlled" and check_reference:
            reference = entry.get("reference_run", plan_name + "/" + entry.get("reference", ""))
            parts = reference.replace("\\", "/").split("/")
            require(len(parts) == 2 and parts[1], "Invalid controlled reference")
            reference_plan, reference_id = map(safe_name, parts)
            other_plan, other_digest = self.plan(reference_plan)
            matches = [e for e in other_plan["runs"] if e["id"] == reference_id]
            require(len(matches) == 1 and matches[0].get("policy", "proposed") == "proposed",
                    "Controlled reference is not a planned proposed run")
            other, other_instance = self.load_result(reference_plan, other_plan, other_digest, matches[0], False)
            require(other is not None and other["instance_sha256"] == instance_digest, "Controlled reference input mismatch")
            extract_result(other, other_instance, matches[0])
            require(other.get("metrics", {}).get("passed") is True, "Invalid controlled reference metrics")
            close(result.get("controlled_target"), other["metrics"]["served"], "controlled reference target")
        return result, instance

    def collect_plan(self, name):
        plan, digest = self.plan(name)
        directory = self.root / "runs" / name
        progress = self.reader.json(directory / "progress.json", optional=True)
        ids = {entry["id"] for entry in plan["runs"]}
        completed, failures, active = set(), {}, None
        if progress is not None:
            require(progress.get("plan_sha256") == digest and progress.get("purpose") == plan["purpose"],
                    f"Progress plan mismatch: {name}")
            require(normalise_hashes(progress.get("source_hashes"), source=True) == self.sources,
                    f"Progress module hash mismatch: {name}")
            require(progress.get("solver_version") == self.manifest["packages"]["cplex"],
                    f"Progress solver version differs from freeze: {name}")
            completed_list = progress.get("completed")
            require(isinstance(completed_list, list) and len(set(completed_list)) == len(completed_list),
                    "Duplicate or missing progress completion list")
            completed = set(completed_list)
            require(isinstance(progress.get("failures"), list), "Missing failure list")
            for failure in progress["failures"]:
                require(failure["id"] not in failures and isinstance(failure.get("error"), str) and failure["error"],
                        "Duplicate or unexplained progress failure")
                failures[failure["id"]] = failure["error"]
            active = progress.get("active")
            require(completed <= ids and failures.keys() <= ids and (active is None or active in ids),
                    "Progress contains an unplanned id")
            require(not completed.intersection(failures), "Run is both completed and failed")
            require(active not in completed, "Completed run marked active")
            require(type(progress.get("finished", False)) is bool, "Invalid finished flag")
        if directory.exists():
            extras = [p.name for p in directory.iterdir() if p.is_dir() and p.name not in ids
                      and any((p / f).exists() for f in ("result.json", "provenance.json", "instance.json"))]
            require(not extras, f"Unplanned run directories in {name}: {extras}")
        issues, rows = [], []
        for ordinal, entry in enumerate(plan["runs"], 1):
            run_id = entry["id"]
            target = directory / run_id
            row = {"plan": name, "plan_sha256": digest, "ordinal": ordinal, "run_id": run_id,
                   "algorithm": entry["algorithm"], "policy": entry.get("policy", "proposed"),
                   "limit_seconds_per_stage": entry["seconds"], "planned_entry": entry,
                   "settings": self.settings(entry), "record_state": None,
                   "has_final_result": False, "progress_completed": run_id in completed,
                   "failure": failures.get(run_id), "instance_sha256": None,
                   "result_path": None, "result_sha256": None, "partial_artifacts": None,
                   **dict.fromkeys(METRIC_FIELDS)}
            for key in self.setting_names:
                row["setting_" + key] = row["settings"][key]
            try:
                result, instance = self.load_result(name, plan, digest, entry)
                if result is not None:
                    require(run_id not in failures, "Failed run also has a final result")
                    row.update(extract_result(result, instance, entry))
                    row.update({"record_state": "result" if run_id in completed else "unreported_result",
                                "has_final_result": True, "instance_sha256": result["instance_sha256"],
                                "result_path": (target / "result.json").relative_to(self.root).as_posix(),
                                "result_sha256": self.reader.hashes[(target / "result.json").resolve()]})
                    if run_id not in completed:
                        issues.append(f"{run_id}: final result not yet acknowledged in progress")
                else:
                    pending_provenance = self.reader.json(target / "provenance.json", optional=True)
                    if pending_provenance is not None:
                        self.provenance(pending_provenance, plan, digest, entry)
                    partial = [f for f in ("provenance.json", "instance.json", "stage1.json", "partial.json") if (target / f).exists()]
                    row["partial_artifacts"] = partial or None
                    if run_id in failures:
                        row["record_state"] = "failure"
                    elif run_id in completed:
                        row["record_state"] = "missing_result"
                    elif run_id == active:
                        row["record_state"] = "active"
                    else:
                        row["record_state"] = "missing_result" if partial else "unattempted"
                    if run_id not in failures:
                        issues.append(f"{run_id}: {row['record_state']}")
            except CollectionError as exc:
                if isinstance(exc, SnapshotChanged):
                    raise
                raise CollectionError(f"{name}/{run_id}: {exc}") from exc
            rows.append(row)
        finished = progress is not None and progress.get("finished") is True
        if not finished:
            issues.insert(0, "Plan is not marked finished")
        if active is not None:
            issues.insert(0, "Plan has an active run")
        if completed | failures.keys() != ids:
            issues.insert(0, "Progress does not account for every planned run")
        summary = {"name": name, "purpose": plan["purpose"], "sha256": digest, "planned_runs": len(rows),
                   "progress_finished": finished, "active": active, "record_counts": dict(Counter(r["record_state"] for r in rows)),
                   "result_count": sum(r["has_final_result"] for r in rows), "failure_count": len(failures),
                   "timeout_count": sum(r["any_timeout"] is True for r in rows),
                   "certified_count": sum(r["source_certified"] is True for r in rows),
                   "complete": not issues, "incomplete_reasons": issues}
        return rows, summary


def collect(root, plans, allow_incomplete=False):
    require(plans and len(plans) == len(set(plans)), "Specify distinct named plans explicitly")
    for attempt in range(3):
        try:
            collector = Collector(root)
            rows, summaries = [], []
            for name in plans:
                plan_rows, summary = collector.collect_plan(name)
                rows.extend(plan_rows)
                summaries.append(summary)
            collector.reader.check_unchanged()
            complete = all(p["complete"] for p in summaries)
            require(allow_incomplete or complete,
                    "Final collection refused: " + "; ".join(f"{p['name']}: {p['result_count']}/{p['planned_runs']} results, "
                    f"finished={p['progress_finished']}, failures={p['failure_count']}" for p in summaries if not p["complete"]))
            return {"schema_version": 1, "scope": SCOPE, "status": "incomplete" if allow_incomplete else "final",
                    "is_final": not allow_incomplete, "allow_incomplete": allow_incomplete,
                    "all_plans_complete": complete, "selected_plans": list(plans),
                    "created_utc": datetime.now(timezone.utc).isoformat(), "freeze": "formal_v1",
                    "source_hashes": collector.sources, "collector_sha256": sha256(read_bytes(Path(__file__))),
                    "frozen_environment": {key: collector.manifest[key] for key in ("python", "platform", "packages")},
                    "null_encoding_csv": "null", "caveats": CAVEATS, "plans": summaries,
                    "planned_runs": len(rows), "rows": rows,
                    "input_sha256": {p.relative_to(collector.root).as_posix(): digest
                                     for p, digest in sorted(collector.reader.hashes.items())},
                    "supporting_reference_plans": sorted(set(collector.plan_cache) - set(plans))}
        except SnapshotChanged:
            if attempt == 2:
                raise
            time.sleep(0.1)


def _atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_outputs(snapshot, output, root):
    output, root = Path(output).resolve(), Path(root).resolve()
    for folder in ("runs", "plans", "evacuation", "data", "frozen", "tests"):
        protected = root / folder
        require(not output.is_relative_to(protected), f"Output may not overwrite research inputs: {output}")
    output.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    columns = ["collection_status", "collection_is_final"] + list(snapshot["rows"][0])
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in snapshot["rows"]:
        values = {"collection_status": snapshot["status"], "collection_is_final": snapshot["is_final"], **row}
        writer.writerow({k: json.dumps(v, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                         if v is None or isinstance(v, (dict, list, bool)) else v for k, v in values.items()})
    csv_bytes = buffer.getvalue().encode("utf-8")
    snapshot = {**snapshot, "csv_file": "runs.csv", "csv_sha256": sha256(csv_bytes), "csv_columns": columns}
    json_bytes = (json.dumps(snapshot, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write(output / "runs.csv", csv_bytes)
    # JSON is the commit record and binds the CSV bytes if a reader races publication.
    _atomic_write(output / "results.json", json_bytes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plans", nargs="+", required=True, help="Exact plan basenames, without .json (no globbing)")
    parser.add_argument("--output-dir", required=True, type=Path, help="Destination for runs.csv and results.json")
    parser.add_argument("--allow-incomplete", action="store_true", help="Development snapshot, always labelled incomplete")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    try:
        snapshot = collect(root, args.plans, args.allow_incomplete)
        write_outputs(snapshot, args.output_dir, root)
    except (CollectionError, OSError, KeyError, TypeError, ZeroDivisionError, StopIteration) as exc:
        print(f"Collection rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": snapshot["status"], "planned_runs": snapshot["planned_runs"],
                      "plans": [{k: p[k] for k in ("name", "result_count", "planned_runs", "timeout_count", "complete")}
                                for p in snapshot["plans"]], "output_dir": str(args.output_dir.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
