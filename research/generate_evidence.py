from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import warnings
from pathlib import Path
from statistics import mean, median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np


NETWORKS = ("ND", "SF", "Anaheim", "Winnipeg")
COLOURS = ("#2166ac", "#b2182b", "#238b45", "#7b5c37")
POLICIES = ("proposed", "aggregate", "aggregate_fair", "controlled", "proportional",
            "weighted", "coverage", "leximin")
POLICY_LABELS = {"proposed": "Proposed", "aggregate": "Aggregate",
                 "aggregate_fair": "Aggregate + floor", "controlled": "Matched coverage",
                 "proportional": "Proportional", "weighted": "Weighted",
                 "coverage": "Floor + coverage", "leximin": "Leximin"}
EXPECTED = {"core_v1": 96, "policies_v1": 84, "direct_comparison_v1": 48,
            "ablation_v1": 20, "groups_v1": 102, "assistance_v1": 60, "modelling_v1": 44}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configure_fonts():
    path = font_manager.findfont("Arial", fallback_to_default=False)
    if "arial" not in Path(path).name.lower():
        raise ValueError(f"Arial did not resolve to the required font: {path}")
    plt.rcParams.update({"font.family": "Arial", "font.size": 13, "axes.labelsize": 13,
                         "axes.titlesize": 14, "axes.titleweight": "bold", "axes.unicode_minus": False,
                         "mathtext.fontset": "custom", "mathtext.rm": "Arial",
                         "mathtext.it": "Arial:italic", "mathtext.bf": "Arial:bold",
                         "mathtext.fallback": None, "pdf.fonttype": 42, "ps.fonttype": 42,
                         "savefig.facecolor": "white", "axes.spines.top": False,
                         "axes.spines.right": False, "legend.frameon": False})
    return path


def select(rows, **conditions):
    return [row for row in rows if all(row.get(key) == value for key, value in conditions.items())]


def central(rows, network, policy="proposed"):
    selected = select(rows, setting_network=network, policy=policy)
    if policy == "proposed":
        selected = select(selected, plan="core_v1", setting_pattern="concentrated",
                          setting_scarcity=0.8, setting_loss=0.0)
    else:
        selected = select(selected, plan="policies_v1")
    if len(selected) != 3 or {r["setting_replication"] for r in selected} != {1, 2, 3}:
        raise ValueError(f"Expected three central rows: {network}, {policy}")
    return sorted(selected, key=lambda r: r["setting_replication"])


def finite_number(value, name, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or (nonnegative and value < 0)):
        raise ValueError(f"Missing or invalid numeric value: {name}")
    return value


def controlled_without_incumbent(row):
    stages = row.get("stages")
    return (row.get("plan") == "policies_v1" and row.get("policy") == "controlled"
            and row.get("has_final_result") is True and row.get("progress_completed") is True
            and row.get("failure") is None and row.get("source_certified") is False
            and row.get("validation_passed") is None and row.get("any_timeout") is True
            and row.get("stage_count") == 1 and row.get("incumbent_stage_count") == 0
            and isinstance(stages, list) and len(stages) == 1 and isinstance(stages[0], dict)
            and stages[0].get("status_code") == 108 and stages[0].get("name") == "travel"
            and stages[0].get("callback_error") is None and stages[0].get("certified") is False
            and all(stages[0].get(key) is None for key in ("incumbent", "validation_max"))
            and all(row.get(key) is None for key in (
                "attained_z", "service", "fine_service", "fine_z", "coverage_fraction",
                "travel_person_minutes", "validation_max")))


def ordered_bounds(record, lower="lower_bound", upper="upper_bound"):
    low = finite_number(record.get(lower), lower)
    high = finite_number(record.get(upper), upper)
    if low > high:
        raise ValueError("Internally crossed objective interval; recollect the audited result")
    return low, high


def secondary_safeguard_stop(row):
    stages = row.get("stages")
    values = row.get("objective_intervals")
    if not (row.get("policy") == "proposed" and row.get("has_final_result") is True
            and row.get("progress_completed") is True and row.get("failure") is None
            and row.get("validation_passed") is True and row.get("source_certified") is False
            and row.get("stage_count") == 2 and row.get("incumbent_stage_count") in (1, 2)
            and isinstance(stages, list) and len(stages) == 2
            and all(isinstance(stage, dict) for stage in stages)
            and isinstance(values, list) and len(values) == 2
            and all(isinstance(value, dict) for value in values)):
        return False
    first, second = stages
    primary, secondary = values
    error = second.get("callback_error")
    return (first.get("name") == "floor" and first.get("callback_error") is None
            and first.get("incumbent") is not None
            and second.get("name") == "travel" and second.get("status_code") in (113, 114)
            and second.get("certified") is False and isinstance(error, str)
            and error.startswith("RuntimeError('Invalid Farkas ray:")
            and (second.get("incumbent") is not None) == (second["status_code"] == 113)
            and row["incumbent_stage_count"] == (2 if second["status_code"] == 113 else 1)
            and primary.get("name") == "floor" and primary.get("bound_usable") is True
            and secondary.get("name") == "travel" and secondary.get("bound_usable") is False
            and all(secondary.get(key) is None for key in
                    ("lower_bound", "upper_bound", "absolute_gap", "relative_gap"))
            and all(row.get("secondary_" + key) is None for key in
                    ("lower_bound", "upper_bound", "absolute_gap", "relative_gap")))


def matched_instances(first, second):
    digest = first.get("instance_sha256")
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(char not in "0123456789abcdef" for char in digest) or
            digest != second.get("instance_sha256")):
        raise ValueError("Comparison requires matching physical-instance hashes")
    if first.get("validation_passed") is not True or second.get("validation_passed") is not True:
        raise ValueError("Comparison requires validated returned allocations")


def objective_interval(row, name):
    values = row.get("objective_intervals")
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError("Missing objective intervals")
    candidates = [value for value in values if value.get("name") == name]
    if len(candidates) != 1:
        raise ValueError(f"Required {name} interval is absent or not unique")
    return candidates[0]


def stats(values):
    values = [finite_number(value, "observation") for value in values]
    if not values:
        raise ValueError("Cannot summarise absent or non-finite observations")
    return {"n": len(values), "mean": mean(values), "minimum": min(values), "maximum": max(values),
            "median": median(values)}


def intervals(rows):
    lows = [r["attained_z"] for r in rows]
    highs = [r["maxmin_upper_bound"] for r in rows]
    if any(value is None for value in lows + highs):
        raise ValueError("A max-min interval is unavailable")
    return mean(lows), min(lows), max(highs)


def gap_max(row):
    values = row.get("objective_intervals")
    if not isinstance(values, list) or not values:
        raise ValueError(f"Incomplete objective gaps: {row.get('run_id')}")
    gaps = [finite_number(interval.get("relative_gap"), "objective relative gap", True)
            for interval in values]
    return max(gaps)


def primary_closed(row):
    absolute = finite_number(row.get("maxmin_absolute_gap"), "max-min absolute gap", True)
    relative = finite_number(row.get("maxmin_relative_gap"), "max-min relative gap", True)
    return absolute <= 1.00001e-9 or relative <= 1.00001e-4


def status_key(fig, envelope=True, attained_range=False, bound_bars=False, heatmap=False):
    handles = [Line2D([], [], color="#444444", marker="o", ls="none", label="Primary gap closed"),
               Line2D([], [], color="#444444", marker="o", mfc="white", ls="none",
                      label="Primary gap open" + (" (also heatmap *)" if heatmap else ""))]
    if envelope:
        handles.append(Patch(facecolor="#777777", alpha=0.15, label="Draw-and-bound envelope"))
    if attained_range:
        handles.append(Line2D([], [], color="#777777", marker="|", ls="none",
                              label="Partition bars: attained draw range"))
    if bound_bars:
        handles.append(Line2D([], [], color="#777777", marker="|", ls="none",
                              label="Bars: attained floor to primary upper bound"))
    fig.legend(handles=handles, loc="outside lower center", ncol=2, fontsize=11)


def conditional_floor_difference(proposed, aggregate):
    if proposed["policy"] != "proposed" or aggregate["policy"] != "aggregate_fair":
        raise ValueError("Wrong policies for conditional floor comparison")
    matched_instances(proposed, aggregate)
    if tuple(proposed.get("primary_" + key) for key in ("name", "sense", "units", "scope")) != (
            "floor", "max", "service_ratio", "unrestricted_maxmin_within_declared_finite_model"):
        raise ValueError("Proposed interval is not the unrestricted max-min objective")
    floor = objective_interval(aggregate, "floor")
    if (floor["sense"], floor["units"], floor["scope"], floor["bound_usable"]) != (
            "max", "service_ratio", "conditional_on_preserved_targets", True):
        raise ValueError("Invalid conditional floor interval")
    targets = floor.get("targets")
    if not isinstance(targets, dict) or set(targets) != {"aggregate_fair"}:
        raise ValueError("Conditional floor must retain its recorded aggregate target")
    target = finite_number(targets["aggregate_fair"], "aggregate coverage target", True)
    proposed_lower, proposed_upper = ordered_bounds(proposed, "maxmin_lower_bound", "maxmin_upper_bound")
    aggregate_lower, aggregate_upper = ordered_bounds(floor)
    lower = proposed_lower - aggregate_upper
    upper = proposed_upper - aggregate_lower
    if upper < -1e-7 or lower > upper + 1e-7:
        raise ValueError("Cross-policy floor bounds contradict feasible-set inclusion")
    return {"conditional_floor_difference_lower": max(0.0, lower),
            "conditional_floor_difference_upper": max(0.0, upper),
            "conditional_aggregate_coverage_target": target,
            "aggregate_fair_floor_lower": floor["lower_bound"],
            "aggregate_fair_floor_upper": floor["upper_bound"],
            "conditional_difference_roundoff_widened": upper < 0,
            "conditional_difference_scope": "unrestricted_maxmin_minus_floor_at_recorded_aggregate_target"}


def matched_travel_comparison(proposed, controlled):
    if controlled_without_incumbent(controlled):
        instance_hash = proposed.get("instance_sha256")
        if (proposed.get("validation_passed") is not True or proposed.get("policy") != "proposed"
                or not isinstance(instance_hash, str) or len(instance_hash) != 64
                or any(char not in "0123456789abcdef" for char in instance_hash)
                or instance_hash != controlled.get("instance_sha256")
                or abs(finite_number(controlled.get("controlled_target"), "controlled target", True)
                       - finite_number(proposed.get("coverage_fraction"), "proposed coverage", True)) > 1e-7):
            raise ValueError("No-incumbent comparison lacks a validated matched reference")
        return {"travel_absolute_difference_person_minutes": None,
                "travel_increase_vs_control_incumbent_percent": None,
                "travel_increase_lower_percent": None, "travel_increase_upper_percent": None,
                "travel_ratio_unavailable_reason": "controlled_time_limit_without_incumbent",
                "travel_bounds_roundoff_widened": False,
                "travel_ratio_scope": "unavailable_no_controlled_incumbent"}
    matched_instances(proposed, controlled)
    value = objective_interval(controlled, "travel")
    if (value["name"], value["sense"], value["units"], value["scope"], value["bound_usable"]) != (
            "travel", "min", "normalised_person_hours", "matched_coverage_policy", True):
        raise ValueError("Wrong matched-coverage travel interval")
    target = finite_number(controlled.get("controlled_target"), "controlled target", True)
    coverage = finite_number(proposed.get("coverage_fraction"), "proposed coverage", True)
    if proposed["policy"] != "proposed" or controlled["policy"] != "controlled" or abs(target - coverage) > 1e-7:
        raise ValueError("Controlled target differs from the proposed allocation")
    demand = finite_number(proposed.get("total_demand"), "total demand", True)
    if demand == 0:
        raise ValueError("Total demand must be positive")
    cost = finite_number(proposed.get("travel_person_minutes"), "proposed travel", True) / (60 * demand)
    finite_number(cost, "normalised proposed travel", True)
    lower, upper = ordered_bounds(value)
    if min(cost, lower, upper) < -1e-7 or lower > min(upper, cost) + 1e-7 * max(1, abs(cost)):
        raise ValueError("Cross-policy travel bounds are inconsistent")
    widened = lower < 0 or upper < 0 or lower > cost
    lower, upper = max(0.0, min(lower, cost)), max(0.0, upper)
    result = {"travel_absolute_difference_person_minutes": (cost - upper) * 60 * proposed["total_demand"],
              "travel_increase_vs_control_incumbent_percent": None,
              "travel_increase_lower_percent": None, "travel_increase_upper_percent": None,
              "travel_ratio_unavailable_reason": None, "travel_bounds_roundoff_widened": widened,
              "travel_ratio_scope": "returned_proposed_allocation_vs_matched_coverage_travel_optimum"}
    if cost == 0 or upper == 0:
        result["travel_ratio_unavailable_reason"] = "zero_reference_or_proposed_travel"
        return result
    result["travel_increase_vs_control_incumbent_percent"] = 100 * (cost / upper - 1)
    result["travel_increase_lower_percent"] = max(0, 100 * (cost / min(upper, cost) - 1))
    if lower > 0:
        result["travel_increase_upper_percent"] = max(0, 100 * (cost / lower - 1))
    else:
        result["travel_ratio_unavailable_reason"] = "nonpositive_lower_bound_for_upper_ratio"
    return result


def curve(ax, groups, x, colour, label, marker="o", linestyle="-"):
    values = [intervals(group) for group in groups]
    centre, lower, upper = map(np.asarray, zip(*values))
    ax.plot(x, centre, color=colour, lw=1.7, ls=linestyle, marker=marker, ms=6, markevery=[], label=label)
    ax.fill_between(x, lower, upper, color=colour, alpha=0.13, linewidth=0)
    for xx, yy, group in zip(x, centre, groups):
        ax.plot(xx, yy, marker=marker, ms=6.2, mec=colour, mew=1.5,
                mfc=colour if all(primary_closed(r) for r in group) else "white")
    ax.grid(axis="y", alpha=0.2, zorder=0)
    ax.set_axisbelow(True)


def csv_export(path, rows, columns):
    records = []
    for row in rows:
        record = {}
        for key in columns:
            value = row[key]
            encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
            record[key] = "null" if value is None else encoded if isinstance(value, (list, dict)) else value
        records.append(record)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def trace_points(stage):
    points = []
    previous = -1.0
    for record in stage["trace"]:
        value, bound, seconds = record["incumbent"], record["bound"], record["seconds"]
        finite_number(seconds, "callback seconds", True)
        if seconds < previous:
            raise ValueError("Decreasing recorded callback times")
        previous = seconds
        for name, item in (("trace incumbent", value), ("trace bound", bound)):
            if item is not None:
                finite_number(item, name)
        if value is None or bound is None:
            continue
        if abs(value) > 1e-10 and abs(bound) < 1e20:
            points.append((seconds, max(0.01, 100 * abs(bound - value) / abs(value))))
    if not points or any(b[0] < a[0] for a, b in zip(points, points[1:])):
        raise ValueError("Primary trace is empty or has decreasing recorded times")
    return points


class Evidence:
    def __init__(self, collection, output):
        self.collection_path = collection.resolve()
        collection_bytes = collection.read_bytes()
        self.collection = json.loads(collection_bytes)
        if (self.collection.get("is_final") is not True or self.collection.get("all_plans_complete") is not True or
                self.collection.get("allow_incomplete") is not False or self.collection.get("status") != "final" or
                self.collection.get("schema_version") != 1 or
                self.collection.get("scope") != "floating-point tolerance-certified finite path/time model"):
            raise ValueError("Only a complete, audited collection may generate manuscript evidence")
        json.dumps(self.collection, allow_nan=False)
        self.rows = self.collection["rows"]
        for name, count in EXPECTED.items():
            selected = select(self.rows, plan=name)
            if len(selected) != count:
                raise ValueError(f"Wrong declared scope for {name}")
            if any(not r["has_final_result"] or not (r["validation_passed"] is True
                       or controlled_without_incumbent(r)) for r in selected):
                raise ValueError(f"Unavailable or unvalidated outcome in {name}; resolve reporting before plotting")
        if len(self.rows) != sum(EXPECTED.values()):
            raise ValueError("Unexpected additional runs in collection")
        if len({row["run_id"] for row in self.rows}) != len(self.rows):
            raise ValueError("Duplicate run identifiers")
        for row in self.rows:
            if controlled_without_incumbent(row):
                for key in ("total_demand", "seconds", "nodes"):
                    finite_number(row.get(key), f"{row['run_id']}.{key}", True)
                continue
            safeguarded = secondary_safeguard_stop(row)
            if row["incumbent_stage_count"] != row["stage_count"] and not safeguarded:
                raise ValueError(f"A required stage lacks an incumbent: {row['run_id']}; preserve this outcome in reporting")
            for interval in row["objective_intervals"]:
                required = ("lower_bound", "upper_bound", "absolute_gap", "relative_gap")
                if safeguarded and interval["name"] == "travel":
                    continue
                if not interval["bound_usable"] or any(interval[key] is None for key in required):
                    raise ValueError(f"Unusable {interval['name']} interval in {row['run_id']}; do not plot as zero")
                if not all(math.isfinite(interval[key]) for key in required):
                    raise ValueError(f"Non-finite objective interval: {row['run_id']}")
                ordered_bounds(interval)
                finite_number(interval["absolute_gap"], "objective absolute gap", True)
            if not safeguarded:
                gap_max(row)
            for key in ("attained_z", "fine_z", "coverage_fraction", "total_demand", "travel_person_minutes",
                        "seconds", "nodes", "validation_max"):
                finite_number(row.get(key), f"{row['run_id']}.{key}", True)
            for key in ("service", "fine_service"):
                values = row.get(key)
                if not isinstance(values, list) or not values:
                    raise ValueError(f"Missing {key} observations: {row['run_id']}")
                for value in values:
                    finite_number(value, f"{row['run_id']}.{key}", True)
            if row["primary_scope"] == "unrestricted_maxmin_within_declared_finite_model":
                primary_closed(row)
                ordered_bounds(row, "maxmin_lower_bound", "maxmin_upper_bound")
            if row["policy"] == "proposed" and not safeguarded:
                finite_number(row.get("secondary_relative_gap"), "conditional travel gap", True)
        if digest(collection.with_name(self.collection["csv_file"])) != self.collection["csv_sha256"]:
            raise ValueError("Collection CSV does not match the committed JSON")
        self.by_id = {r["run_id"]: r for r in self.rows}
        for network in NETWORKS:
            for policy in POLICIES:
                central(self.rows, network, policy)
            for proposed, aggregate, controlled in zip(central(self.rows, network),
                    central(self.rows, network, "aggregate_fair"), central(self.rows, network, "controlled")):
                conditional_floor_difference(proposed, aggregate)
                matched_travel_comparison(proposed, controlled)
        self.traces = {}
        research = Path(__file__).resolve().parent
        for identifier in ("direct_Anaheim_concentrated_moderate_r1", "core_Anaheim_concentrated_moderate_r1"):
            row = self.by_id[identifier]
            path = (research / row["result_path"]).resolve()
            if not path.is_relative_to((research / "runs").resolve()):
                raise ValueError("Trace source is outside the run directory")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["result_sha256"]:
                raise ValueError(f"Trace source changed after collection: {identifier}")
            stage = json.loads(raw)["stage1"]
            if not stage["trace"] or stage["relative_gap"] is None:
                raise ValueError(f"Required primary trace is unavailable: {identifier}")
            finite_number(stage["relative_gap"], "final primary-stage gap", True)
            finite_number(stage["seconds"], "whole-stage seconds", True)
            trace_points(stage)
            self.traces[identifier] = stage
        self.output = output.resolve()
        if self.output.exists() and any(self.output.iterdir()):
            raise FileExistsError("Evidence output must be a new empty directory")
        self.output.mkdir(parents=True, exist_ok=True)
        self.font = configure_fonts()
        self.figures = []
        self.summary = {"collection_sha256": hashlib.sha256(collection_bytes).hexdigest(), "planned_runs": sum(EXPECTED.values()),
                        "completed_runs": len(self.rows), "networks": {}, "figures": self.figures}

    def save(self, fig, name, used, section):
        directory = self.output / section
        directory.mkdir(parents=True, exist_ok=True)
        pdf, png = directory / f"{name}.pdf", directory / f"{name}.png"
        text_fonts = sorted({text.get_fontfamily()[0] for text in fig.findobj(matplotlib.text.Text)
                             if text.get_text()})
        if text_fonts != ["Arial"]:
            raise ValueError(f"Unexpected fonts in {name}: {text_fonts}")
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            fig.savefig(pdf, bbox_inches="tight", pad_inches=0.06)
            fig.savefig(png, dpi=300, bbox_inches="tight", pad_inches=0.06)
        glyph_warnings = [str(item.message) for item in seen if "glyph" in str(item.message).lower()]
        if glyph_warnings:
            raise ValueError(f"Missing figure glyphs: {glyph_warnings}")
        input_rows = [{"run_id": row["run_id"], "result_sha256": row["result_sha256"]} for row in used]
        record = {"name": name, "section": section, "pdf_sha256": digest(pdf), "png_sha256": digest(png),
                  "font": self.font, "text_fonts": text_fonts, "inputs": input_rows,
                  "collection_sha256": self.summary["collection_sha256"]}
        (directory / f"{name}_provenance.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        self.figures.append(record)
        plt.close(fig)

    def policy_comparisons(self):
        chosen = ("proposed", "aggregate", "aggregate_fair", "coverage", "leximin")
        labels = ("Proposed", "Aggregate", "Aggregate\n+ floor", "Floor\n+ coverage", "Leximin")
        fig, axes = plt.subplots(2, 2, figsize=(8.8, 7.2), layout="constrained")
        used, all_rows, contrasts = [], [], []
        for ax, network, letter in zip(axes.flat, NETWORKS, "abcd"):
            groups = [central(self.rows, network, policy) for policy in chosen]
            matrix = np.asarray([np.mean([r["service"] for r in group], axis=0) for group in groups]).T
            artist = ax.imshow(matrix, cmap="YlGnBu", vmin=0, vmax=1, aspect="auto")
            status_labels = [label + ("*" if not all(r["source_certified"] for r in group) else "")
                             for label, group in zip(labels, groups)]
            ax.set_xticks(range(len(labels)), status_labels, fontsize=10.5,
                          rotation=35, ha="right", rotation_mode="anchor")
            ax.set_yticks(range(3), ["General", "Older", "Specialised"], fontsize=11)
            ax.set_title(f"({letter}) {network}", loc="left", pad=12)
            for y in range(3):
                for x in range(len(chosen)):
                    ax.text(x, y, f"{matrix[y, x]:.3f}", ha="center", va="center", fontsize=12,
                            color="white" if matrix[y, x] > 0.52 else "#17212b")
            for i, group in enumerate(groups):
                used.extend(group)
            for policy in POLICIES:
                group = central(self.rows, network, policy)
                available = [r for r in group if r["validation_passed"] is True]
                complete = len(available) == len(group)
                record = {"network": network, "policy": policy, "certified": sum(r["source_certified"] for r in group),
                          "n": len(group), "returned_allocations": len(available),
                          "z": stats(r["attained_z"] for r in group) if complete else None,
                          "coverage": stats(r["coverage_fraction"] for r in group) if complete else None,
                          "travel_person_minutes": stats(r["travel_person_minutes"] for r in group) if complete else None,
                          "largest_objective_gap_percent": max(gap_max(r) for r in group) * 100 if complete else None,
                          "mean_group_service": np.mean([r["service"] for r in group], axis=0).tolist() if complete else None}
                all_rows.append(record)
            for proposed, aggregate, controlled in zip(central(self.rows, network),
                    central(self.rows, network, "aggregate_fair"), central(self.rows, network, "controlled")):
                if len({r["instance_sha256"] for r in (proposed, aggregate, controlled)}) != 1:
                    raise ValueError("Policy comparison uses unmatched physical instances")
                contrasts.append({"network": network, "replication": proposed["setting_replication"],
                    "instance_sha256": proposed["instance_sha256"],
                    "proposed_z": proposed["attained_z"], "proposed_upper_z": proposed["maxmin_upper_bound"],
                    "aggregate_fair_attained_z": aggregate["attained_z"],
                    "attained_difference": proposed["attained_z"] - aggregate["attained_z"],
                    "difference_upper_vs_returned_benchmark": proposed["maxmin_upper_bound"] - aggregate["attained_z"],
                    **conditional_floor_difference(proposed, aggregate),
                    **matched_travel_comparison(proposed, controlled)})
        fig.colorbar(artist, ax=axes, label="Mean served fraction", fraction=0.027, pad=0.025)
        fig.legend(handles=[Line2D([], [], color="#444444", marker="$*$", ls="none",
                   label="At least one complete policy objective sequence remains uncertified")],
                   loc="outside lower center", fontsize=10.5)
        self.summary["policy_summary"] = all_rows
        self.summary["policy_contrasts"] = contrasts
        self.save(fig, "policy_group_service", used, "5.2_policy_comparisons")

    def resources(self):
        fig, axes = plt.subplots(3, 2, figsize=(9.2, 10.7), layout="constrained")
        used = []
        for ax, network, letter in zip(axes.flat, NETWORKS, "abcd"):
            x = (0.6, 0.8, 1.0)
            for pattern, colour, marker in (("balanced", COLOURS[0], "o"), ("concentrated", COLOURS[1], "s")):
                groups = [select(self.rows, plan="core_v1", setting_network=network, setting_pattern=pattern,
                                 setting_loss=0.0, setting_scarcity=value) for value in x]
                curve(ax, groups, x, colour, pattern.capitalize(), marker, "-" if pattern == "balanced" else "--")
                used.extend(row for group in groups for row in group)
            ax.set_title(f"({letter}) {network}", loc="left")
            ax.set_xticks(x)
            ax.set_xlabel("Scarcity multiplier " + r"$\alpha$")
            ax.set_ylabel("Minimum served fraction")
            ax.legend(loc="best", fontsize=11)
        matrix, differences, closed = [], [], []
        for network in NETWORKS:
            groups = [select(self.rows, plan="core_v1", setting_network=network, setting_pattern="concentrated",
                             setting_scarcity=0.8, setting_loss=loss) for loss in (0.0, 0.25, 0.5)]
            means = [mean(r["attained_z"] for r in group) for group in groups]
            matrix.append(means)
            differences.append([value - means[0] for value in means])
            closed.append([all(primary_closed(r) for r in group) for group in groups])
            used.extend(row for group in groups for row in group)
        ax = axes[2, 0]
        artist = ax.imshow(matrix, cmap="YlGnBu", vmin=0, vmax=max(map(max, matrix)) * 1.1, aspect="auto")
        ax.set_xticks(range(3), ["0", "0.25", "0.50"])
        ax.set_yticks(range(4), NETWORKS)
        ax.set_xlabel("Capacity-loss fraction " + r"$\sigma$")
        ax.set_title("(e) Concentrated demand", loc="left")
        for y in range(4):
            for x in range(3):
                ax.text(x, y, f"{matrix[y][x]:.3f}" + ("" if closed[y][x] else "*") + (f"\n({differences[y][x]:+.3f})" if x else ""),
                        ha="center", va="center", fontsize=11,
                        color="white" if matrix[y][x] > max(map(max, matrix)) * 0.6 else "#17212b")
        ax = axes[2, 1]
        for network, colour, marker in zip(NETWORKS, COLOURS, ("o", "s", "^", "D")):
            x = (0.1, 0.2, 0.35, 0.5, 0.8)
            groups = [central(self.rows, network) if beta == 0.5 else select(self.rows,
                plan="assistance_v1", setting_network=network, setting_assistance=beta) for beta in x]
            curve(ax, groups, x, colour, network, marker)
            shared = select(self.rows, plan="assistance_v1", setting_network=network, setting_assistance=None)
            ax.axhline(mean(r["attained_z"] for r in shared), color=colour, ls=":", lw=1.2, alpha=0.8)
            ax.plot(x[-1], mean(r["attained_z"] for r in shared), marker=marker, ms=6,
                    mec=colour, mfc=colour if all(primary_closed(r) for r in shared) else "white")
            used.extend(shared)
            used.extend(row for group in groups for row in group)
        ax.set_title("(f) Assistance supply", loc="left")
        ax.set_xlabel("Assistance multiplier " + r"$\beta$")
        ax.set_ylabel("Minimum served fraction")
        ax.set_xticks([0.1, 0.35, 0.5, 0.8])
        handles, labels = ax.get_legend_handles_labels()
        handles.append(Line2D([], [], color="#555555", ls=":", label="Shared resources only"))
        ax.legend(handles=handles, ncol=2, loc="best", fontsize=10.5)
        status_key(fig, heatmap=True)
        self.summary["capacity_loss_means"] = dict(zip(NETWORKS, matrix))
        self.summary["capacity_loss_changes"] = dict(zip(NETWORKS, differences))
        self.summary["capacity_loss_all_primary_closed"] = dict(zip(NETWORKS, closed))
        self.save(fig, "resource_sensitivity", used, "5.3_resource_sensitivity")

    def groups(self):
        fig, axes = plt.subplots(3, 2, figsize=(9.2, 10.7), layout="constrained")
        used = []
        for i, network in enumerate(("SF", "Winnipeg")):
            for j, field in enumerate(("concentration", "assisted_share")):
                ax = axes[j, i]
                selected = [r for r in select(self.rows, plan="groups_v1", setting_network=network)
                            if r["setting_" + field] is not None]
                x = sorted({r["setting_" + field] for r in selected})
                groups = [select(selected, **{"setting_" + field: value}) for value in x]
                curve(ax, groups, x, COLOURS[i], network)
                ax.set_xlabel(r"Concentration parameter $q_{\mathrm{c}}$" if j == 0
                              else r"Specialised-assistance prevalence $p_{\mathrm{a}}$")
                ax.set_title(f"({'abcdef'[2*j+i]}) {network}", loc="left")
                ax.set_ylabel("Minimum served fraction")
                if j == 0:
                    ax.set_xticks([0, 0.6, 1.2, 1.8])
                used.extend(selected)
            ax = axes[2, i]
            partition = ("two_fine", "three_fine", "six", "rare")
            groups = [select(self.rows, plan="groups_v1", setting_network=network, setting_partition=value)
                      for value in partition]
            for offset, colour, field, label, marker in ((-0.06, COLOURS[0], "attained_z", "Protected groups", "o"),
                                                       (0.06, COLOURS[1], "fine_z", "Elementary categories", "s")):
                averages = [mean(r[field] for r in group) for group in groups]
                lower = [min(r[field] for r in group) for group in groups]
                upper = [max(r[field] for r in group) for group in groups]
                positions = np.arange(4) + offset
                ax.plot(positions[:3], averages[:3], color=colour, marker=marker, markevery=[], label=label, ms=6)
                ax.errorbar(positions, averages, [np.subtract(averages, lower), np.subtract(upper, averages)],
                            fmt="none", color=colour, capsize=3)
                for xx, yy, group in zip(positions, averages, groups):
                    ax.plot(xx, yy, marker=marker, ms=6, mec=colour, mew=1.4,
                            mfc=colour if all(primary_closed(r) for r in group) else "white")
            ax.axvline(2.5, color="#999999", ls=":", lw=1)
            ax.set_xticks(range(4), ["2", "3", "6", "6, rare"])
            ax.set_xlabel("Number of protected groups")
            ax.set_ylabel("Minimum served fraction")
            ax.set_title(f"({'abcdef'[4+i]}) {network}", loc="left")
            ax.grid(axis="y", alpha=0.2)
            ax.legend(fontsize=11, loc="best")
            used.extend(row for group in groups for row in group)
        for ax in axes.flat:
            ax.set_ylim(-0.015, 0.30)
            ax.set_yticks([0, 0.10, 0.20, 0.30])
            ax.ticklabel_format(axis="y", style="plain", useOffset=False)
        status_key(fig, attained_range=True)
        self.save(fig, "group_assumptions", used, "5.4_group_assumptions")

    def modelling(self):
        fig, axes = plt.subplots(2, 2, figsize=(9.2, 7.0), layout="constrained")
        used = []
        defaults = {"k": 5, "detour": 1.5, "delta": 15, "window": 90}
        labels = {"k": "Paths per origin-shelter pair", "detour": "Maximum detour ratio",
                  "delta": "Departure-bin width (min)", "window": "Departure window (min)"}
        for ax, field, letter in zip(axes.flat, defaults, "abcd"):
            for network, colour, marker in zip(NETWORKS, COLOURS, ("o", "s", "^", "D")):
                selected = [r for r in select(self.rows, plan="modelling_v1", setting_network=network)
                            if r["setting_" + field] != defaults[field]]
                selected += [central(self.rows, network)[0]]
                selected.sort(key=lambda r: r["setting_" + field])
                x = [r["setting_" + field] for r in selected]
                y = [r["attained_z"] for r in selected]
                upper = [r["maxmin_upper_bound"] for r in selected]
                ax.plot(x, y, color=colour, label=network, lw=1.6, marker=marker, markevery=[], ms=6)
                ax.errorbar(x, y, [np.zeros(len(y)), np.subtract(upper, y)], fmt="none", color=colour, capsize=3)
                for xx, yy, row in zip(x, y, selected):
                    ax.plot(xx, yy, marker=marker, ms=6, mec=colour,
                            mfc=colour if primary_closed(row) else "white", mew=1.3)
                used.extend(selected)
            ax.set_title(f"({letter}) {labels[field]}", loc="left")
            ax.set_xlabel(labels[field])
            ax.set_ylabel("Minimum served fraction")
            ax.set_xticks(x)
            ax.grid(axis="y", alpha=0.2)
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="outside upper center", ncol=4, fontsize=12)
        status_key(fig, envelope=False, bound_bars=True)
        self.save(fig, "modelling_sensitivity", used, "5.5_modelling_choices")

    def performance(self):
        direct = select(self.rows, plan="direct_comparison_v1")
        pairs = []
        for row in direct:
            identifier = row["planned_entry"]["paired_benders"].split("/", 1)[1]
            other = self.by_id[identifier]
            if row["instance_sha256"] != other["instance_sha256"]:
                raise ValueError("Direct and Benders physical instances differ")
            pairs.append((row, other))
        fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.3), layout="constrained")
        performance = []
        for i, algorithm in enumerate(("direct", "benders_flow")):
            counts, times = [], []
            for network in NETWORKS:
                group = [pair[i] for pair in pairs if pair[i]["setting_network"] == network]
                counts.append(sum(r["source_certified"] for r in group))
                times.append(median(r["seconds"] for r in group))
                performance.append({"network": network, "algorithm": algorithm, "n": len(group),
                    "certified": counts[-1], "seconds": stats(r["seconds"] for r in group),
                    "primary_gap_percent": stats(100 * r["maxmin_relative_gap"] for r in group),
                    "secondary_gap_percent": stats(100 * r["secondary_relative_gap"] for r in group),
                    "nodes": stats(r["nodes"] for r in group)})
            x = np.arange(4) + (i - 0.5) * 0.34
            label = "Direct MILP" if i == 0 else "Benders"
            bars = axes[0].bar(x, counts, width=0.33, color=COLOURS[i], label=label)
            axes[0].bar_label(bars, labels=[str(value) for value in counts], padding=4, fontsize=11)
            bars = axes[1].bar(x, times, width=0.33, color=COLOURS[i], label=label)
            axes[1].bar_label(bars, labels=[f"{value:.2f}" if value < 10 else f"{value:.0f}" for value in times],
                               padding=5, fontsize=10)
        axes[0].set_ylim(0, 15)
        axes[0].set_yticks(range(0, 13, 3))
        axes[0].set_ylabel("Certified two-stage runs (out of 12)")
        axes[0].set_title("(a) Completed optimality checks", loc="left")
        axes[1].set_yscale("log")
        axes[1].set_ylabel("Median total runtime (s, log scale)")
        axes[1].set_title("(b) Runtime under fixed stage limits", loc="left")
        for ax in axes:
            ax.set_xticks(range(4), NETWORKS)
            ax.grid(axis="y", alpha=0.2)
            ax.set_axisbelow(True)
        axes[0].legend(fontsize=11, ncol=2, loc="upper center")
        self.summary["performance"] = performance
        self.save(fig, "algorithm_comparison", [r for pair in pairs for r in pair], "5.6_computational_performance")

    def gap_trace(self):
        rows = [self.by_id["direct_Anaheim_concentrated_moderate_r1"],
                self.by_id["core_Anaheim_concentrated_moderate_r1"]]
        fig, ax = plt.subplots(figsize=(9.2, 4.5), layout="constrained")
        trace_summary = []
        for row, colour in zip(rows, COLOURS):
            stage = self.traces[row["run_id"]]
            points = trace_points(stage)
            x, y = zip(*points)
            label = "Direct MILP" if row["algorithm"] == "direct" else "Benders"
            label += f" (final stage gap {100 * stage['relative_gap']:.3g}%)"
            ax.step(x, y, where="post", color=colour, label=label, lw=1.8)
            ax.plot(x[-1], y[-1], "o", color=colour, ms=6)
            trace_summary.append({"run_id": row["run_id"], "final_stage_relative_gap": stage["relative_gap"],
                                  "last_callback_seconds": x[-1], "whole_stage_seconds": stage["seconds"],
                                  "display_floor_percent": 0.01, "recorded_points": len(points)})
        ax.axhline(0.01, color="#666666", ls=":", lw=1.2, label="Display floor / relative tolerance (0.01%)")
        ax.set_yscale("log")
        ax.set_xlabel("Recorded primary-stage search time (s)")
        ax.set_ylabel("Primary gap (%, log scale)")
        ax.grid(axis="y", alpha=0.2, which="both")
        ax.legend(fontsize=11, loc="best")
        self.summary["gap_traces"] = trace_summary
        self.save(fig, "anaheim_gap_progress", rows, "5.6_computational_performance")

    def tables(self):
        directory = self.output / "tables"
        directory.mkdir(exist_ok=True)
        lines = [r"\begingroup\small\color{darkred}", r"\setlength{\tabcolsep}{3pt}",
                 r"\renewcommand{\arraystretch}{1.10}",
                 r"\begin{longtable}{@{}>{\raggedright\arraybackslash}p{0.105\textwidth}>{\raggedright\arraybackslash}p{0.185\textwidth}*{2}{>{\centering\arraybackslash}p{0.105\textwidth}}>{\centering\arraybackslash}p{0.155\textwidth}*{2}{>{\centering\arraybackslash}p{0.105\textwidth}}@{}}",
                 r"\caption{\rev{Policy outcomes over three paired demand draws. Plans and certified counts retain all three runs. Means and maximum gaps are omitted when any draw lacks an allocation. Max. gap includes every final policy objective, with later objectives conditional on preserved targets. Closure permits relative tolerance \(10^{-4}\) or absolute tolerance \(10^{-9}\) in normalised units.}}\label{tab:all-policies}\\",
                 r"\toprule Network & Policy & Mean minimum & Mean coverage & Mean travel ($10^3$ person-min) & Plans / certified & Max. gap (\%) \\",
                 r"\midrule\endfirsthead",
                 r"\toprule Network & Policy & Mean minimum & Mean coverage & Mean travel ($10^3$ person-min) & Plans / certified & Max. gap (\%) \\",
                 r"\midrule\endhead", r"\bottomrule\endfoot"]
        for row in self.summary["policy_summary"]:
            minimum = "--" if row["z"] is None else f"{row['z']['mean']:.3f}"
            coverage = "--" if row["coverage"] is None else f"{row['coverage']['mean']:.3f}"
            travel = "--" if row["travel_person_minutes"] is None else f"{row['travel_person_minutes']['mean']/1000:.2f}"
            gap = "--" if row["largest_objective_gap_percent"] is None else f"{row['largest_objective_gap_percent']:.3f}"
            lines.append(f"{row['network']} & {POLICY_LABELS[row['policy']]} & {minimum} & {coverage} & {travel} & "
                         f"{row['returned_allocations']}/{row['n']}, {row['certified']}/{row['n']} & {gap}" + r" \\")
        lines += [r"\end{longtable}\endgroup", ""]
        (directory / "policy_summary.tex").write_text("\n".join(lines), encoding="utf-8")
        ablation = []
        for row in select(self.rows, plan="ablation_v1"):
            ablation.append({key: row[key] for key in ("run_id", "setting_network", "algorithm", "seconds",
                            "attained_z", "maxmin_upper_bound", "maxmin_relative_gap", "secondary_relative_gap",
                            "nodes", "integer_calls", "fractional_calls", "feasibility_cuts", "optimality_cuts",
                            "lp_seconds", "source_certified", "validation_max", "validation_passed", "stages")})
        self.summary["ablation"] = ablation
        allocations = [r for r in self.rows if r["validation_passed"] is True]
        unavailable = [r["run_id"] for r in self.rows if controlled_without_incumbent(r)]
        self.summary["validation"] = {"all_passed": len(allocations) + len(unavailable) == len(self.rows),
                                      "scope": "all_returned_allocations",
                                      "returned_allocations": len(allocations),
                                      "no_incumbent_timeouts": unavailable,
                                      "numerical_safeguard_terminations": [
                                          {"run_id": row["run_id"], "stage": stage["name"],
                                           "status_code": stage["status_code"], "callback_error": stage["callback_error"]}
                                          for row in self.rows if secondary_safeguard_stop(row)
                                          for stage in row["stages"] if stage.get("callback_error") is not None],
                                      "maximum_residual": max(r["validation_max"] for r in allocations)}
        csv_export(directory / "ablation.csv", ablation, list(ablation[0]))
        csv_export(directory / "policies.csv", self.summary["policy_summary"], list(self.summary["policy_summary"][0]))
        csv_export(directory / "policy_contrasts.csv", self.summary["policy_contrasts"], list(self.summary["policy_contrasts"][0]))
        csv_export(directory / "performance.csv", self.summary["performance"], list(self.summary["performance"][0]))
        columns = ["run_id", "plan", "policy", "algorithm", "settings", "attained_z", "maxmin_upper_bound",
                   "service", "fine_service", "coverage_fraction", "travel_person_minutes", "source_certified",
                   "primary_relative_gap", "secondary_relative_gap", "seconds", "result_sha256"]
        csv_export(self.output / "reported_runs.csv", self.rows, columns)

    def run(self):
        self.policy_comparisons()
        self.resources()
        self.groups()
        self.modelling()
        self.performance()
        self.gap_trace()
        self.tables()
        self.summary["generator_sha256"] = digest(__file__)
        (self.output / "evidence_summary.json").write_text(json.dumps(self.summary, indent=2, allow_nan=False) + "\n",
                                                          encoding="utf-8")
        print(f"Generated {len(self.figures)} figures from {len(self.rows)} audited formal runs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    Evidence(args.collection, args.output).run()


if __name__ == "__main__":
    main()
