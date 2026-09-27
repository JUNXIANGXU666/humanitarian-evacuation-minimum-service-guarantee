from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import cplex
import numpy
import scipy

from evacuation.data import Settings, build_instance
from evacuation.solver import solve, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", choices=["ND", "SF", "Anaheim", "Winnipeg"], required=True)
    parser.add_argument("--algorithm", choices=["direct", "benders_basic", "benders_bounds", "benders_fractional", "benders_warm", "benders_flow"], default="direct")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--assistance", type=float)
    parser.add_argument("--scarcity", type=float, default=0.8)
    parser.add_argument("--replication", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.output / "result.json").exists():
        raise FileExistsError("Completed run exists. Use a new run ID or read its result.")
    instance = build_instance(Settings(args.network, scarcity=args.scarcity,
                                       assistance=args.assistance, replication=args.replication))
    root = Path(__file__).parent
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((root / "evacuation").glob("*.py"))}
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "provenance.json", {"python": sys.version, "executable": sys.executable,
               "platform": platform.platform(), "cplex": cplex.__version__, "numpy": numpy.__version__,
               "scipy": scipy.__version__, "code_hashes": hashes, "command": sys.argv})
    result = solve(instance, args.algorithm, args.seconds, args.output)
    print(json.dumps({"network": args.network, "algorithm": args.algorithm, "certified": result["certified"],
                      "seconds": result["total_seconds"], "z": result.get("metrics", {}).get("z"),
                      "stage1_gap": result["stage1"]["relative_gap"],
                      "stage2_gap": result["stage2"]["relative_gap"] if result["stage2"] else None}))


if __name__ == "__main__":
    main()
