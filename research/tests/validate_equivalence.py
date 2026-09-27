from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evacuation.data import Instance
from evacuation.policies import solve_policy
from evacuation.solver import solve, write_json
from validate_algorithms import one_path


def mixed_instance(seed, assistance):
    rng = np.random.default_rng(seed)
    d = np.array([[0.3, 0.15, 0.1], [0.25, 0.15, 0.05]]) * rng.uniform(0.8, 1.2, (2, 3))
    d /= d.sum()
    return Instance({"network": "mixed_validation", "delta": 15.0}, [1, 2], [3], ["General", "Older", "Assisted"],
                    d.tolist(), 1000.0,
                    [{"tail": 1, "head": 3, "time": 6.0}, {"tail": 2, "head": 3, "time": 12.0},
                     {"tail": 1, "head": 2, "time": 6.0}],
                    [{"origin": 0, "shelter": 0, "nodes": [1, 3], "arcs": [0], "time": 6.0, "offsets": [[0, 0]]},
                     {"origin": 1, "shelter": 0, "nodes": [2, 3], "arcs": [1], "time": 12.0, "offsets": [[1, 0]]},
                     {"origin": 0, "shelter": 0, "nodes": [1, 2, 3], "arcs": [2, 1], "time": 18.0,
                      "offsets": [[2, 0], [1, 0], [1, 1]]}],
                    2, 4, rng.uniform(0.12, 0.28, 3).tolist(), [0.8], [1.0, 1.0],
                    [[0.0, 1.0, 2.0], [0.0, 0.0, 1.0]],
                    [[0.08, 0.06], [0.03, 0.02]] if assistance else [], [[0], [1], [2]], [2], [], 3, 3, {})


def main():
    out = Path(sys.argv[1])
    if (out / "summary.json").exists():
        raise FileExistsError("Completed validation exists")
    checks = []
    for seed in range(4):
        for assistance in (False, True):
            instance = mixed_instance(seed, assistance)
            results = {}
            for algorithm in ["direct", "benders_basic", "benders_flow"]:
                result = solve(instance, algorithm, 30, out / f"mixed_{seed}_{int(assistance)}_{algorithm}")
                assert result["certified"], (seed, assistance, algorithm)
                results[algorithm] = result
            reference = results["direct"]
            for algorithm, result in results.items():
                primary_error = abs(reference["metrics"]["z"] - result["metrics"]["z"])
                travel_error = abs(reference["metrics"]["travel_normalised_minutes"] - result["metrics"]["travel_normalised_minutes"])
                assert primary_error < 2e-6 and travel_error < 2e-5, (primary_error, travel_error)
                checks.append({"seed": seed, "assistance": assistance, "algorithm": algorithm,
                               "primary_error": primary_error, "travel_error": travel_error, "passed": True})
    instance = one_path(0.9, 0.12)
    for policy in ["aggregate", "weighted", "proportional", "coverage", "leximin", "controlled"]:
        result = solve_policy(instance, policy, 30, out / f"policy_{policy}", target=0.3 if policy == "controlled" else None)
        assert result["certified"], policy
        if policy in ("leximin", "coverage"):
            assert np.max(np.abs(np.asarray(result["metrics"]["service"]) - [1.0, 0.3, 0.3])) < 3e-6
        if policy == "proportional":
            assert abs(result["metrics"]["z"] - 0.3) < 2e-6
        checks.append({"policy": policy, "metrics": result["metrics"], "passed": True})
    write_json(out / "summary.json", {"passed": len(checks), "total": len(checks), "checks": checks})
    print(f"{len(checks)}/{len(checks)} checks passed", flush=True)


if __name__ == "__main__":
    main()
