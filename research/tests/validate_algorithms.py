from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evacuation.data import Instance
from evacuation.formulation import Formulation
from evacuation.solver import Separation, solve, write_json


def one_path(capacity, assistance=None):
    return Instance({"network": "known_answer", "delta": 15.0}, [1], [2], ["General", "Older", "Assisted"],
                    [[0.7, 0.2, 0.1]], 1000.0,
                    [{"tail": 1, "head": 2, "time": 10.0, "capacity": 1000.0}],
                    [{"origin": 0, "shelter": 0, "nodes": [1, 2], "arcs": [0],
                      "time": 10.0, "offsets": [[0, 0]]}],
                    1, 2, [capacity], [1.0], [1.0], [[0.0, 1.0, 2.0]],
                    [] if assistance is None else [[assistance]], [[0], [1], [2]], [1], [], 2, 2, {})


def main():
    root = Path(sys.argv[1])
    if (root / "summary.json").exists():
        raise FileExistsError("Completed validation already exists")
    root.mkdir(parents=True, exist_ok=True)
    checks = []
    for name, capacity, assistance, answer in [
        ("road_bottleneck", 0.6, None, 0.6),
        ("assistance_bottleneck", 0.9, 0.12, 0.3),
        ("saturated", 1.0, None, 1.0),
        ("no_road", 0.0, None, 0.0),
    ]:
        instance = one_path(capacity, assistance)
        for algorithm in ["direct", "benders_basic", "benders_bounds", "benders_fractional", "benders_warm", "benders_flow"]:
            result = solve(instance, algorithm, 15, root / f"{name}_{algorithm}")
            observed = result["metrics"]["z"]
            passed = result["certified"] and abs(observed - answer) <= 2e-7
            checks.append({"test": name, "algorithm": algorithm, "expected_z": answer,
                           "observed_z": observed, "passed": passed})
            if not passed:
                raise AssertionError(checks[-1])
    f = Formulation(one_path(0.6))
    engine = Separation(f, 2)
    v = np.zeros(f.nv)
    v[0] = 0.0
    v[f.ny:f.ny + f.nb] = [0.35, 0.10, 0.05]
    row = engine.separate(v)
    assert row is not None and row.name == "infeasible"
    for fraction in np.linspace(0, 0.6, 13):
        point = v.copy()
        point[0] = 1.0
        point[f.ny:f.ny + f.nb] = np.array([0.7, 0.2, 0.1]) * fraction
        point[f.eta] = 10.0 / 60.0 * fraction
        assert sum(point[i] * a for i, a in zip(row.indices, row.values)) <= row.rhs + 1e-9
    checks.append({"test": "farkas_cut_feasible_points", "points": 13, "passed": True})
    v[0] = 1.0
    cut = engine.separate(v)
    assert cut is not None and cut.name == "optimal"
    for fraction in np.linspace(0, 0.6, 13):
        point = v.copy()
        point[f.ny:f.ny + f.nb] = np.array([0.7, 0.2, 0.1]) * fraction
        point[f.eta] = 10.0 / 60.0 * fraction
        assert sum(point[i] * a for i, a in zip(cut.indices, cut.values)) <= cut.rhs + 1e-9
    checks.append({"test": "optimality_cut_feasible_points", "points": 13, "passed": True})
    engine.lp.end()
    write_json(root / "summary.json", {"checks": checks, "passed": len(checks), "total": len(checks)})
    print(json.dumps({"passed": len(checks), "total": len(checks)}))


if __name__ == "__main__":
    main()
