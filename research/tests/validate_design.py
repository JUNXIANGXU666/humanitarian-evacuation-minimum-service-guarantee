from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evacuation.data import Settings, build_instance
from evacuation.policies import solve_policy
from evacuation.solver import write_json
from validate_algorithms import one_path


def main():
    out = Path(sys.argv[1])
    if (out / "summary.json").exists():
        raise FileExistsError("Completed design validation exists")
    checks = []
    for network in ("ND", "SF", "Anaheim", "Winnipeg"):
        base = build_instance(Settings(network, replication=1, assistance=0.5))
        for delta in (5, 10, 15, 30):
            case = build_instance(Settings(network, replication=1, assistance=0.5, delta=delta))
            assert case.total_demand == base.total_demand
            assert np.allclose(case.demand, base.demand, atol=0, rtol=0)
            assert np.allclose(case.capacities, base.capacities, atol=0, rtol=0)
            assert sum(case.response) == sum(base.response)
            assert np.allclose(np.sum(case.assistance_capacity, axis=1), np.sum(base.assistance_capacity, axis=1))
            supplies = np.asarray(case.assistance_capacity)
            response = np.asarray(case.response)
            assert np.all(supplies[:, response == 0] == 0)
            for row in supplies:
                active = response > 0
                assert np.allclose(row[active] / response[active], row.sum() / response.sum())
            checks.append({"network": network, "delta": delta, "test": "fixed_physical_inputs", "passed": True})
        fine = [build_instance(Settings(network, replication=1, assistance=0.5, partition=p))
                for p in ("two_fine", "three_fine", "six")]
        for case in fine:
            assert case.demand == fine[0].demand
            assert case.assistance_weights == fine[0].assistance_weights
            assert case.assistance_capacity == fine[0].assistance_capacity
        checks.append({"network": network, "test": "fixed_population_partition", "passed": True})
    result = solve_policy(one_path(0.9, 0.12), "aggregate_fair", 30, out / "aggregate_fair")
    assert result["certified"]
    assert abs(result["metrics"]["served"] - 0.82) < 3e-7
    assert result["metrics"]["z"] < 3e-6
    checks.append({"test": "aggregate_fair_known_answer", "passed": True})
    write_json(out / "summary.json", {"passed": len(checks), "total": len(checks), "checks": checks})
    print(f"{len(checks)}/{len(checks)} checks passed", flush=True)


if __name__ == "__main__":
    main()
