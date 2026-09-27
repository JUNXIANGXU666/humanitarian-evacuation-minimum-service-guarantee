from __future__ import annotations

import argparse
from pathlib import Path

from evacuation.solver import write_json


NETWORKS = ("ND", "SF", "Anaheim", "Winnipeg")
CELLS = [
    ("balanced_base", "balanced", 1.0, 0.0),
    ("balanced_moderate", "balanced", 0.8, 0.0),
    ("balanced_severe", "balanced", 0.6, 0.0),
    ("concentrated_base", "concentrated", 1.0, 0.0),
    ("concentrated_moderate", "concentrated", 0.8, 0.0),
    ("concentrated_severe", "concentrated", 0.6, 0.0),
    ("loss_moderate", "concentrated", 0.8, 0.25),
    ("loss_severe", "concentrated", 0.8, 0.5),
]


def core_plan():
    runs = []
    for network in NETWORKS:
        for name, pattern, scarcity, loss in CELLS:
            for replication in (1, 2, 3):
                settings = {"network": network, "pattern": pattern, "scarcity": scarcity,
                            "loss": loss, "replication": replication, "assistance": 0.5}
                runs.append({"id": f"core_{network}_{name}_r{replication}", "settings": settings,
                             "algorithm": "benders_flow", "seconds": 600, "family": "core"})
    return {"purpose": "Formal revised computational evidence. Eight scenario cells per network with three paired demand draws.",
            "replications": [1, 2, 3], "runs": runs}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Refusing to replace a frozen plan")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, core_plan())


if __name__ == "__main__":
    main()
