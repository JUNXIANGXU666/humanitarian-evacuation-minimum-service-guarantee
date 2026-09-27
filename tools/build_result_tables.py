from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from evidence_contract import validate_evidence


ROOT = Path(__file__).resolve().parents[1]
NETWORKS = ("ND", "SF", "Anaheim", "Winnipeg")
VARIANTS = {"benders_basic": "Basic", "benders_bounds": "Bounds",
            "benders_fractional": "Fractional", "benders_warm": "Warm start",
            "benders_flow": "Aggregate flow"}
FIELDS = ("seconds", "maxmin_relative_gap", "secondary_relative_gap", "integer_calls",
          "fractional_calls", "feasibility_cuts", "optimality_cuts")


def safeguarded_secondary(record):
    stages = record.get("stages")
    if not (record.get("source_certified") is False and record.get("validation_passed") is True
            and isinstance(stages, list) and len(stages) == 2
            and all(isinstance(stage, dict) for stage in stages)):
        return False
    second = stages[1]
    error = second.get("callback_error")
    interval = second.get("interval", {})
    return (stages[0].get("name") == "floor" and stages[0].get("callback_error") is None
            and second.get("name") == "travel" and second.get("status_code") in (113, 114)
            and second.get("certified") is False and isinstance(error, str)
            and error.startswith("RuntimeError('Invalid Farkas ray:")
            and isinstance(interval, dict) and interval.get("bound_usable") is False
            and all(interval.get(key) is None for key in
                    ("lower_bound", "upper_bound", "absolute_gap", "relative_gap")))


def checked_rows(records):
    expected = {(network, variant) for network in NETWORKS for variant in VARIANTS}
    indexed = {}
    for record in records:
        key = (record["setting_network"], record["algorithm"])
        if key not in expected or key in indexed:
            raise ValueError("Ablation must contain one record per declared network and variant")
        if safeguarded_secondary(record) and record["secondary_relative_gap"] is not None:
            raise ValueError("A numerical-safeguard termination cannot have a fabricated secondary gap")
        for field in FIELDS:
            value = record[field]
            if field == "secondary_relative_gap" and value is None and safeguarded_secondary(record):
                continue
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid ablation value: {field}")
            if field.endswith(("calls", "cuts")) and int(value) != value:
                raise ValueError(f"Nonintegral ablation count: {field}")
        indexed[key] = record
    if set(indexed) != expected:
        raise ValueError("All twenty declared ablation outcomes are required")
    return [indexed[(network, variant)] for network in NETWORKS for variant in VARIANTS]


def percent_gap(value):
    percentage = 100 * value
    return r"\(<0.001\)" if 0 < percentage < 0.001 else f"{percentage:.3f}"


def render_ablation(records):
    rows = checked_rows(records)
    lines = [r"\begingroup\small\color{darkred}", r"\setlength{\tabcolsep}{3pt}",
             r"\renewcommand{\arraystretch}{1.10}",
             r"\begin{longtable}{@{}>{\raggedright\arraybackslash}p{0.105\textwidth}>{\raggedright\arraybackslash}p{0.160\textwidth}>{\centering\arraybackslash}p{0.090\textwidth}>{\centering\arraybackslash}p{0.105\textwidth}>{\centering\arraybackslash}p{0.120\textwidth}>{\centering\arraybackslash}p{0.085\textwidth}>{\centering\arraybackslash}p{0.120\textwidth}>{\centering\arraybackslash}p{0.115\textwidth}@{}}",
             r"\caption{\rev{Algorithm ablation on four fixed instances with a 180-second limit per optimisation stage.}}\label{tab:ablation-results}\\",
             r"\toprule Network & Variant & Time (s) & Primary gap (\%) & Secondary gap (\%) & Integer calls & Fractional calls & Cuts F/O \\",
             r"\midrule\endfirsthead",
             r"\toprule Network & Variant & Time (s) & Primary gap (\%) & Secondary gap (\%) & Integer calls & Fractional calls & Cuts F/O \\",
             r"\midrule\endhead", r"\bottomrule\endfoot", r"\bottomrule",
             r"\multicolumn{8}{@{}p{0.97\textwidth}@{}}{\footnotesize Variants add the listed features cumulatively. Time sums both stage timers and warm-start work. Guard denotes numerical-safeguard termination, with no usable secondary gap and time measured to termination. The primary gap uses the final returned service floor. The secondary gap is conditional on the transferred floor. Calls count callbacks, including cache hits. F/O gives recorded feasibility/optimality cuts across both stages. Positive gaps below 0.001\% are shown as \(<0.001\).}\\",
             r"\endlastfoot"]
    previous = None
    for row in rows:
        network = row["setting_network"]
        if previous is not None and network != previous:
            lines.append(r"\addlinespace[3pt]")
        secondary = "Guard" if row["secondary_relative_gap"] is None else percent_gap(row["secondary_relative_gap"])
        runtime = f"{row['seconds']:.2f}" if row["seconds"] < 1 else f"{row['seconds']:.1f}"
        lines.append(f"{network} & {VARIANTS[row['algorithm']]} & {runtime} & "
                     f"{percent_gap(row['maxmin_relative_gap'])} & {secondary} & "
                     f"{int(row['integer_calls'])} & {int(row['fractional_calls'])} & "
                     f"{int(row['feasibility_cuts'])}/{int(row['optimality_cuts'])}" + r" \\")
        previous = network
    lines += [r"\end{longtable}\endgroup", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build manuscript tables from complete audited evidence.")
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    before = validate_evidence(ROOT, evidence)
    raw = evidence.read_bytes()
    summary = json.loads(raw)
    collection = json.loads((ROOT / before["collection"]["path"]).read_bytes())
    actual = {row["run_id"]: row for row in collection["rows"] if row["plan"] == "ablation_v1"}
    reported = summary["ablation"]
    if (len(reported) != len(actual) or {row["run_id"] for row in reported} != set(actual) or
            any(any(key not in actual[row["run_id"]] or value != actual[row["run_id"]][key]
                    for key, value in row.items()) for row in reported)):
        raise ValueError("Ablation summary does not match the strict collected outcomes")
    output = ROOT / "manuscript/generated/ablation_summary.tex"
    if output.exists():
        raise FileExistsError("The generated table already exists. Preserve it and review any requested regeneration.")
    text = render_ablation(reported)
    after = validate_evidence(ROOT, evidence)
    if before != after or evidence.read_bytes() != raw:
        raise ValueError("Formal evidence changed while constructing the table")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    report = {"status": "generated_requires_visual_audit", "final_delivery_ready": False,
              "evidence_contract": before, "rows": 20,
              "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "table": output.relative_to(ROOT).as_posix(),
              "table_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    (ROOT / "audit/ablation_table_generation.json").write_text(json.dumps(report, indent=2) + "\n",
                                                             encoding="utf-8")
    print("Generated twenty-row ablation table; compiled-page review remains required")


if __name__ == "__main__":
    main()
