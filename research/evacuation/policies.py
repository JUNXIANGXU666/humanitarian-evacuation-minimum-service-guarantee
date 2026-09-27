from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

import cplex
import numpy as np

from .formulation import Formulation, Row, add_rows
from .solver import Progress, configure, write_json
from .validation import check_policy, gap_closed, objective_gap, policy_value, validate


def _objective(model, coefficients):
    model.objective.set_linear([(i, 0.0) for i in range(model.variables.get_num())])
    model.objective.set_linear([(int(i), float(v)) for i, v in coefficients.items()])


def _stage(model, f, seconds, logfile, maximise, name, policy, protected, target=None):
    log = Path(logfile).open("w", encoding="utf-8")
    configure(model, seconds, log)
    trace = model.register_callback(Progress)
    trace.records, trace.started = [], model.get_time()
    trace.lock = threading.Lock()
    start = time.perf_counter()
    model.solve()
    record = {"name": name, "seconds": time.perf_counter() - start, "limit_seconds": seconds,
              "status": model.solution.get_status_string(), "status_code": model.solution.get_status(),
              "bound": None, "incumbent": None, "relative_gap": None, "absolute_gap": None,
              "solver_relative_gap": None, "certified": False, "maximise": maximise,
              "protected_targets": dict(protected),
              "nodes": model.solution.progress.get_num_nodes_processed(), "validation": None,
              "trace": trace.records}
    raw = None
    if model.solution.is_primal_feasible():
        raw = np.asarray(model.solution.get_values())
        multiplier = -1.0 if maximise else 1.0
        check = validate(f.instance, raw[:f.ny], raw[f.ny:f.ny + f.nb], raw[f.nv:f.nv + f.nx],
                         protected.get("floor", 0.0), master_z=raw[f.z])
        record["validation"] = check
        if not check["passed"]:
            raise RuntimeError(f"Invalid policy solution: {check['violations']}")
        policy_check = check_policy(f.instance, check, policy, protected, target)
        record["policy_validation"] = policy_check
        if not policy_check["passed"]:
            raise RuntimeError(f"Invalid protected policy objectives: {policy_check['violations']}")
        value = policy_value(check, name)
        bound = float(multiplier * model.solution.MIP.get_best_objective())
        record.update({"incumbent": value, "bound": bound, **objective_gap(value, bound, maximise),
                       "solver_relative_gap": float(model.solution.MIP.get_mip_relative_gap())})
        record["certified"] = gap_closed(record)
        record["solution"] = {"y": raw[:f.ny].tolist(), "b": raw[f.ny:f.ny + f.nb].tolist(),
                              "x": raw[f.nv:f.nv + f.nx].tolist()}
    log.close()
    return record, raw


def solve_policy(instance, policy, seconds, directory, target=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    instance.save(directory / "instance.json")
    f = Formulation(instance)
    model = f.create_direct(1)
    served_indices = list(range(f.ny, f.ny + f.nb))
    protected = {}
    records = []
    if policy == "controlled":
        if target is None:
            raise ValueError("Controlled comparison requires matched proposed coverage")
        add_rows(model, [Row(served_indices, [1.0] * f.nb, "E", float(target), "matched_coverage")])
        stages = [(False, "travel", {f.eta: 1.0})]
    elif policy in ("aggregate", "aggregate_fair", "weighted"):
        weights = np.ones(f.ng) if policy != "weighted" else np.arange(1.0, f.ng + 1.0)
        coefficients = {f.ny + o * f.ng + g: -weights[g] for o in range(f.no) for g in range(f.ng)}
        stages = [(True, policy, coefficients), (False, "travel", {f.eta: 1.0})]
        if policy == "aggregate_fair":
            stages.insert(1, (True, "floor", {f.z: -1.0}))
    elif policy == "proportional":
        add_rows(model, [Row([f.ny + j, f.z], [1.0, -float(d)], "E", 0.0, f"proportional_{j}")
                         for j, d in enumerate(f.d.ravel())])
        stages = [(True, "floor", {f.z: -1.0}), (False, "travel", {f.eta: 1.0})]
    elif policy in ("coverage", "leximin"):
        stages = [(True, "floor", {f.z: -1.0})]
        if policy == "coverage":
            stages.append((True, "coverage", {i: -1.0 for i in served_indices}))
        else:
            for k in range(2, len(instance.fairness_groups) + 1):
                index = model.variables.get_num()
                model.variables.add(lb=[0.0], ub=[float(k)], obj=[0.0])
                for subset in itertools.combinations(range(len(instance.fairness_groups)), k):
                    coefs = {index: 1.0}
                    for category in subset:
                        groups = instance.fairness_groups[category]
                        total = f.d[:, groups].sum()
                        for o in range(f.no):
                            for g in groups:
                                j = f.ny + o * f.ng + g
                                coefs[j] = coefs.get(j, 0.0) - 1.0 / total
                    add_rows(model, [Row(list(coefs), list(coefs.values()), "L", 0.0, f"ordered_sum_{k}")])
                stages.append((True, f"ordered_sum_{k}", {index: -1.0}))
        stages.append((False, "travel", {f.eta: 1.0}))
    else:
        raise ValueError(policy)
    raw = None
    for number, (maximise, name, coefficients) in enumerate(stages, 1):
        _objective(model, coefficients)
        record, raw = _stage(model, f, seconds, directory / f"stage{number}.log", maximise, name,
                             policy, protected, target)
        records.append(record)
        write_json(directory / "partial.json", {"policy": policy, "stages": records})
        if raw is None:
            break
        if maximise:
            target_value = record["incumbent"] - 1e-8
            add_rows(model, [Row(list(coefficients), [-float(v) for v in coefficients.values()],
                                "G", target_value, f"preserve_{name}")])
            protected[name] = max(0.0, target_value)
    metrics = next((r["validation"] for r in reversed(records) if r["validation"] is not None), None)
    final_objectives = []
    if metrics is not None:
        for record in records:
            if record["incumbent"] is None:
                continue
            value = policy_value(metrics, record["name"])
            final_objectives.append({"name": record["name"], "achieved": value,
                                     "conditional_bound": record["bound"],
                                     **objective_gap(value, record["bound"], record["maximise"]),
                                     "preserved_target": protected.get(record["name"]),
                                     "target_shortfall": max(0.0, protected.get(record["name"], value) - value)})
    result = {"instance_sha256": instance.digest(), "settings": instance.settings, "algorithm": "direct",
              "policy": policy, "stages": records,
              "certified": bool(len(records) == len(stages) and all(r["certified"] for r in records)
                                and all(gap_closed(r) for r in final_objectives)),
              "metrics": metrics, "final_objectives": final_objectives,
              "total_seconds": sum(r["seconds"] for r in records), "controlled_target": target,
              "dimensions": {"binary": f.ny, "assignment": f.nx, "recourse_rows": len(f.rows)}}
    model.end()
    write_json(directory / "result.json", result)
    return result
