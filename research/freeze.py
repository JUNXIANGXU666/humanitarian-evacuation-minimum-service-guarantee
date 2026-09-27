from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from evacuation.solver import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    directory = root / "frozen" / args.version
    if directory.exists():
        raise FileExistsError(directory)
    gate = json.loads((root.parent / "audit" / "implementation_gate.json").read_text(encoding="utf-8"))
    hashes = {str(p.relative_to(root.parent)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((root / "evacuation").glob("*.py"))}
    audited = {k.replace("\\", "/"): v.lower() for k, v in gate["source_hashes"].items()}
    if gate["verdict"] != "pass" or hashes != audited:
        raise RuntimeError("Implementation gate does not clear the current sources")
    suites = ["known_answers_v5", "equivalence_v3", "design_validation_v3", "guardrails_v1"]
    evidence = {}
    for name in suites:
        file = root / "runs" / name / "summary.json"
        result = json.loads(file.read_text(encoding="utf-8"))
        if result["passed"] != result["total"]:
            raise RuntimeError(f"Failed validation {name}")
        evidence[name] = {"passed": result["passed"], "total": result["total"],
                          "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    files = sorted((root / "evacuation").glob("*.py")) + sorted((root / "tests").glob("*.py"))
    files += sorted(root.glob("*.py")) + sorted((root / "data" / "raw").glob("*"))
    files += [root / "plans" / "core_v1.json"]
    files = [p for p in files if p.is_file()]
    directory.mkdir(parents=True)
    with zipfile.ZipFile(directory / "implementation.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, str(file.relative_to(root)))
    manifest = {"version": args.version, "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_hashes": hashes, "validation": evidence, "gate": gate,
                "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                "packages": {name: importlib.metadata.version(name)
                             for name in ("cplex", "numpy", "scipy", "networkx", "pandas", "matplotlib")},
                "files": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "archive_sha256": hashlib.sha256((directory / "implementation.zip").read_bytes()).hexdigest()}
    write_json(directory / "manifest.json", manifest)
    print(json.dumps({"frozen": args.version, "files": len(files), "checks": sum(x["total"] for x in evidence.values())}))


if __name__ == "__main__":
    main()
