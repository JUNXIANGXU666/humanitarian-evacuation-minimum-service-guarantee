# Results and manuscript map

All paths below refer to the extracted research archive. Start with `evidence/formal_v1/evidence_summary.json` for the combined evidence summary or `RUN_INDEX.csv` to locate individual runs.

| Manuscript section | Evidence | Experiment plans |
| --- | --- | --- |
| 5.1 Implementation validation | `research/runs/known_answers_v5/`, `equivalence_v3/`, `design_validation_v3/`, `guardrails_v1/` | Four implementation-check suites, separate from the 466 formal runs |
| 5.2 Planning policies, Table 4 and Figure 1 | `evidence/formal_v1/tables/policies.csv`, `policy_contrasts.csv`, `policy_summary.tex`, `5.2_policy_comparisons/` | `core_v1`, `policies_v1` |
| 5.3 Scarcity and assistance, Figure 2 | `evidence/formal_v1/5.3_resource_sensitivity/` | `core_v1`, `assistance_v1` |
| 5.4 Population definitions, Figure 3 | `evidence/formal_v1/5.4_group_assumptions/` | `groups_v1` |
| 5.5 Path and time choices, Figure 4 | `evidence/formal_v1/5.5_modelling_choices/` | `core_v1`, `modelling_v1` |
| 5.6 Method comparison, Figures 5 and 6 | `evidence/formal_v1/tables/performance.csv`, `5.6_computational_performance/` | `core_v1`, `direct_comparison_v1` |
| 5.6 Longer allowances, Table 5 | `audit/time_budget_summary_v1/budget_comparison.json`, `budget_summary.tex` | `budget_sensitivity_v1`, paired with the 600-second records |
| 5.6 Decomposition ablation, Table 6 | `evidence/formal_v1/tables/ablation.csv`, `ablation_summary.tex` | `ablation_v1` |

Each figure directory includes the published PDF and PNG and a provenance JSON. The complete parameter grid and method choice for every run are in `research/plans/`. `section_index.json` provides a machine-readable section map.

## Reading one run

For example, `research/runs/core_v1/core_ND_balanced_base_r1/` contains:

- `instance.json`: realised demand, population partition, candidate paths, time grid, road and shelter capacities, and assistance coefficients and supply.
- `provenance.json`: exact plan entry, source hashes and execution environment.
- `result.json`: returned allocation, outcome metrics, certification status, stage bounds and independent feasibility checks.
- Stage JSON and log files: search progress, solver output and callback records for the stages that actually ran.

Flow quantities in the instance and allocation records are normalised by `total_demand`. Multiply them by this total to obtain person counts. Travel objectives are normalised by 60 times total demand. The exported tables already apply their stated units.

Do not infer a missing stage record or null gap to be zero. A numerical-safeguard termination can retain a feasible allocation and primary bounds while leaving the secondary gap unavailable. Three controlled-policy runs completed without an incumbent and remain part of the comparison denominators.

## Methods

The proposed policy uses `benders_flow` for the core, resource, population and modelling-choice blocks. Alternative policies use their direct lexicographic implementations in `research/evacuation/policies.py`. `direct_comparison_v1` uses direct MILP on matched instances. `ablation_v1` contains all five decomposition configurations. The longer-budget plan includes both direct and `benders_flow` procedures.

The recorded service floors, bounds and timing values are the outputs of the released model implementation. No replacement or illustrative numerical data are used in these records.
