from __future__ import annotations

import math

import numpy as np


def objective_gap(value, bound, maximise):
    if not np.isfinite(value) or not np.isfinite(bound):
        raise ValueError("Non-finite objective certificate")
    inversion = value - bound if maximise else bound - value
    if inversion > 1e-7 * max(1.0, abs(value)):
        raise RuntimeError("Returned objective and global bound are inconsistent")
    gap = max(0.0, bound - value if maximise else value - bound)
    return {"absolute_gap": gap, "relative_gap": gap / max(1e-10, abs(value))}


def gap_closed(record):
    return bool(record["absolute_gap"] <= 1.00001e-9 or record["relative_gap"] <= 1.00001e-4)


def validate(instance, y, b, x, floor=0.0, master_z=None):
    """Reconstruct original constraints independently of the sparse model builder."""
    d = np.asarray(instance.demand)
    no, ng = d.shape
    np_, nt = len(instance.paths), instance.departures
    y = np.asarray(y).reshape(np_, nt)
    x = np.asarray(x).reshape(np_, ng, nt)
    b = np.asarray(b).reshape(no, ng)
    inputs = [d, y, b, x, np.asarray(instance.capacities), np.asarray(instance.shelter_capacity),
              np.asarray(instance.response), np.asarray(instance.assistance_weights),
              np.asarray(instance.assistance_capacity), np.asarray([floor]),
              np.asarray([a["time"] for a in instance.arcs])]
    if master_z is not None:
        inputs.append(np.asarray([master_z]))
    if any(not np.all(np.isfinite(values)) for values in inputs):
        return {"passed": False, "violations": {"nonfinite": 1.0}}
    if np.any(d < 0) or np.any(d.sum(axis=0) <= 0):
        return {"passed": False, "violations": {"invalid_demand": 1.0}}
    assigned = np.zeros_like(d)
    shelters = np.zeros(len(instance.shelters))
    arc_loads = {}
    assistance = np.zeros((len(instance.assistance_capacity), nt))
    largest_link = 0.0
    total_time = 0.0
    for p, path in enumerate(instance.paths):
        o, s = path["origin"], path["shelter"]
        for g in range(ng):
            for t in range(nt):
                amount = x[p, g, t]
                assigned[o, g] += amount
                shelters[s] += amount
                total_time += path["time"] * amount
                largest_link = max(largest_link, amount - d[o, g] * y[p, t])
                elapsed = 0.0
                for a in path["arcs"]:
                    end = elapsed + instance.arcs[a]["time"]
                    delta = instance.settings["delta"]
                    for q in range(math.floor(elapsed / delta), math.ceil((end - 1e-10) / delta)):
                        if max(elapsed, q * delta) < min(end, (q + 1) * delta) - 1e-10:
                            arc_loads[a, t + q] = arc_loads.get((a, t + q), 0.0) + amount
                    elapsed = end
                for r in range(len(instance.assistance_capacity)):
                    assistance[r, t] += instance.assistance_weights[r][g] * amount
    service = [float(assigned[:, groups].sum() / d[:, groups].sum()) for groups in instance.fairness_groups]
    fine_service = np.divide(assigned.sum(axis=0), d.sum(axis=0)).tolist()
    violations = {
        "demand_balance": float(np.max(np.abs(assigned - b))),
        "demand_bound": float(max(0, np.max(assigned - d), -np.min(assigned))),
        "arc_capacity": max([0.0] + [value - instance.capacities[a] for (a, t), value in arc_loads.items()]),
        "shelter_capacity": float(max(0, np.max(shelters - instance.shelter_capacity))),
        "response": float(max(0, np.max(y.sum(axis=0) - instance.response))),
        "linking": float(largest_link),
        "nonnegative": float(max(0, -np.min(x), -np.min(b), -np.min(y))),
        "binary": float(np.max(np.abs(y - np.rint(y)))),
        "binary_upper": float(max(0, np.max(y) - 1)),
        "floor": float(max(0, floor - min(service))),
        "master_floor": float(max(0, master_z - min(service))) if master_z is not None else 0.0,
        "assistance": float(max(0, np.max(assistance - instance.assistance_capacity))) if len(assistance) else 0.0,
    }
    tolerance = 1e-7
    return {"passed": bool(max(violations.values()) <= tolerance), "normalised_tolerance": tolerance,
            "violations": violations, "z": min(service), "service": service, "fine_service": fine_service,
            "served": float(assigned.sum()), "travel_normalised_minutes": total_time,
            "unmet": float(1.0 - assigned.sum()), "travel_person_minutes": total_time * instance.total_demand,
            "origin_served": assigned.sum(axis=1).tolist(),
            "served_by_origin_group": assigned.tolist(),
            "assistance_use": assistance.tolist(), "shelter_use": shelters.tolist()}


def policy_value(metrics, name):
    if name == "floor":
        return float(metrics["z"])
    if name in ("aggregate", "aggregate_fair", "coverage"):
        return float(metrics["served"])
    if name == "weighted":
        served = np.asarray(metrics["served_by_origin_group"])
        return float((served @ np.arange(1.0, served.shape[1] + 1.0)).sum())
    if name.startswith("ordered_sum_"):
        k = int(name.rsplit("_", 1)[1])
        return float(sum(sorted(metrics["service"])[:k]))
    if name == "travel":
        return float(metrics["travel_normalised_minutes"] / 60.0)
    raise ValueError(name)


def check_policy(instance, metrics, policy, protected, target=None):
    values = list(protected.values()) + ([] if target is None else [target])
    if not np.all(np.isfinite(values)):
        return {"passed": False, "violations": {"nonfinite_target": 1.0}}
    if policy == "controlled" and target is None:
        return {"passed": False, "violations": {"missing_coverage_target": 1.0}}
    violations = {}
    for name, value in protected.items():
        violations[f"preserve_{name}"] = max(0.0, value - policy_value(metrics, name))
    if policy == "controlled":
        violations["matched_coverage"] = abs(metrics["served"] - target)
    if policy == "proportional":
        served = np.asarray(metrics["served_by_origin_group"])
        violations["proportional"] = float(np.max(np.abs(served - np.asarray(instance.demand) * metrics["z"])))
    return {"passed": max(violations.values(), default=0.0) <= 1e-7, "violations": violations}
