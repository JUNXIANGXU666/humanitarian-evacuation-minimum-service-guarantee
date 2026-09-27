from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evacuation.formulation import Formulation
from evacuation.solver import Separation, write_json
from evacuation.validation import check_policy, gap_closed, objective_gap, policy_value, validate
from validate_algorithms import one_path


def main():
    root = Path(sys.argv[1])
    if (root / "summary.json").exists():
        raise FileExistsError("Completed guardrail validation exists")
    root.mkdir(parents=True, exist_ok=True)
    checks = []
    assert gap_closed(objective_gap(0.5, 0.50005, True))
    assert not gap_closed(objective_gap(0.5 - 1e-8, 0.50005, True))
    assert gap_closed(objective_gap(0.0, 1e-12, True))
    checks.append({"test": "final_gap_and_zero_objective", "passed": True})
    instance = one_path(1.0)
    instance.response = [2.0]
    x = np.array([0.35, 0.10, 0.05])
    valid = validate(instance, [1.0], x, x, master_z=0.5)
    assert valid["passed"]
    checks.append({"test": "feasible_reference", "passed": True})
    for name, y, b, assignment, z in [
        ("binary_upper", [2.0], x, x, 0.5),
        ("binary_noninteger", [0.7], x, x, 0.5),
        ("nonfinite_activation", [np.nan], np.zeros(3), np.zeros(3), 0.0),
        ("nonfinite_assignment", [1.0], x, [np.inf, 0.1, 0.05], 0.5),
        ("master_floor", [1.0], x, x, 0.6),
        ("negative_assignment", [1.0], [-0.1, 0, 0], [-0.1, 0, 0], 0.0),
    ]:
        result = validate(instance, y, b, assignment, master_z=z)
        assert not result["passed"], name
        checks.append({"test": name, "passed": True, "violations": result["violations"]})
    for name in ("floor", "aggregate", "aggregate_fair", "weighted", "coverage", "ordered_sum_2", "ordered_sum_3"):
        target = policy_value(valid, name) + 0.01
        assert not check_policy(instance, valid, "coverage", {name: target})["passed"]
        checks.append({"test": f"reject_lost_{name}", "passed": True})
    assert check_policy(instance, valid, "controlled", {}, 0.5)["passed"]
    assert not check_policy(instance, valid, "controlled", {}, 0.6)["passed"]
    assert not check_policy(instance, valid, "controlled", {}, None)["passed"]
    assert not check_policy(instance, valid, "controlled", {}, np.nan)["passed"]
    checks.append({"test": "controlled_coverage", "passed": True})
    assert check_policy(instance, valid, "proportional", {})["passed"]
    uneven = copy.deepcopy(valid)
    uneven["served_by_origin_group"] = [[0.34, 0.11, 0.05]]
    assert not check_policy(instance, uneven, "proportional", {})["passed"]
    checks.append({"test": "proportional_assignment", "passed": True})
    f = Formulation(one_path(0.5))
    for state in ("infeasible", "optimal"):
        engine = Separation(f, 2)
        dual = np.zeros(len(f.rows))
        if state == "infeasible":
            dual[:f.nb] = 1.0
            dual[next(i for i, row in enumerate(f.rows) if row.name == "arc_0_0")] = -1.0 + 5e-8
        else:
            dual[:f.nb] = 10.0 / 60.0 + 5e-8
        engine.evaluate = lambda v: (state, dual, 0.1, None)
        candidate = np.zeros(f.nv)
        candidate[0] = 1.0
        candidate[f.ny:f.ny + f.nb] = np.array([0.7, 0.2, 0.1]) * 0.6
        row = engine.separate(candidate)
        assert engine.counts["max_cut_correction"] > 0
        for fraction in np.linspace(0, 0.5, 11):
            feasible = candidate.copy()
            feasible[f.ny:f.ny + f.nb] = np.array([0.7, 0.2, 0.1]) * fraction
            feasible[f.eta] = fraction * 10.0 / 60.0
            assert sum(feasible[i] * a for i, a in zip(row.indices, row.values)) <= row.rhs
        engine.lp.end()
        checks.append({"test": f"perturbed_dual_{state}", "passed": True, "points": 11})
    write_json(root / "summary.json", {"passed": len(checks), "total": len(checks), "checks": checks})
    print(f"{len(checks)}/{len(checks)} guardrail checks passed", flush=True)


if __name__ == "__main__":
    main()
