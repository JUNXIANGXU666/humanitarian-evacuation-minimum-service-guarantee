# Reproduction

## Inspect the recorded study

Download and extract `evacuation-research-v1.0.zip`. From its root, run:

```text
python verify_release.py
```

This standard-library check verifies the released files, the frozen scientific modules, every plan, and the provenance and instance identity of all 466 formal runs. No optimisation is performed. `RUN_INDEX.csv` locates each record. JSON result files retain solver status, incumbent, bound, gaps, stopping information and allocation validation. Log files contain the original solver output.

## Environment

The recorded environment used Python 3.11.9, CPLEX 22.2.0.0, NumPy 2.2.6, SciPy 1.13.1, NetworkX 3.4.2, pandas 2.3.1 and Matplotlib 3.10.3. `requirements.txt` lists the non-proprietary Python dependencies. Install the matching CPLEX Python API from a separately licensed installation. A size-limited solver installation may not support these instances.

```text
python -m pip install -r requirements.txt
python -c "import cplex; print(cplex.__version__)"
```

The original host had an Intel Core i7-10750H processor and approximately 16 GB RAM. The master uses four threads and the recourse solver uses one. Runtime depends on the host and solver configuration. Arial was used in the saved figures.

## Create a separate working copy

Preserve the released records. The following command creates a new directory containing the sources, inputs, plans, frozen source metadata and implementation-check records, but no completed formal runs:

```text
python prepare_rerun.py rerun
cd rerun
```

The destination must not already exist. The helper neither changes model settings nor starts the solver.

## Run the plans

Run the following sequentially from the new working-copy root. Core results must exist before the matched-coverage policy runs, which use the exact core allocation as their coverage target.

```text
python -X utf8 -B research/run_plan.py research/plans/core_v1.json research/runs/core_v1
python -X utf8 -B research/run_plan.py research/plans/policies_v1.json research/runs/policies_v1
python -X utf8 -B research/run_plan.py research/plans/direct_comparison_v1.json research/runs/direct_comparison_v1
python -X utf8 -B research/run_plan.py research/plans/ablation_v1.json research/runs/ablation_v1
python -X utf8 -B research/run_plan.py research/plans/groups_v1.json research/runs/groups_v1
python -X utf8 -B research/run_plan.py research/plans/assistance_v1.json research/runs/assistance_v1
python -X utf8 -B research/run_plan.py research/plans/modelling_v1.json research/runs/modelling_v1
```

The saved plans specify 96, 84, 48, 20, 102, 60 and 44 runs respectively. Main runs allow 600 seconds per stage, and ablation runs allow 180 seconds per stage. Proposed-method timers include model setup. Alternative-policy timers surround their solver calls. Warm-start work is included in the proposed-method allowance. Instance generation, final recovery and validation add overhead outside those stage timers.

After the main runs finish, evaluate the fixed follow-up rule. The working-copy helper leaves its output directory and follow-up plan absent so that the decision tool can create fresh records:

```text
python -X utf8 -B tools/assess_time_budget.py --output audit/time_budget_gate_v1
```

If `triggered` is true in the resulting `decision.json`, run the generated twelve-run plan with 3,600 seconds per stage:

```text
python -X utf8 -B research/run_plan.py research/plans/budget_sensitivity_v1.json research/runs/budget_sensitivity_v1
```

The released archive retains the original plan and completed twelve-run comparison. The rule is described in `audit/time_budget_review.md`. Keep different time budgets separate in performance comparisons.

## Collect and plot

After the seven main plans finish, collect their complete records and generate the figures and tables:

```text
python -X utf8 -B research/collect_results.py --plans core_v1 policies_v1 direct_comparison_v1 ablation_v1 groups_v1 assistance_v1 modelling_v1 --output-dir audit/collected_formal_v1
python -X utf8 -B research/generate_evidence.py audit/collected_formal_v1/results.json evidence/formal_v1
python -X utf8 -B tools/build_result_tables.py evidence/formal_v1/evidence_summary.json
python -X utf8 -B tools/budget_evidence.py --output audit/time_budget_summary_v1
```

The collector checks source and plan hashes, matching instances, solver stages and final allocation validation. It rejects incomplete required evidence. It retains completed no-incumbent searches and unavailable secondary gaps without inventing allocation or gap values. The table helper writes the ablation table under `manuscript/generated/` in the working copy.
