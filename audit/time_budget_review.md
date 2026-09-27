# Time-budget comparison protocol

The main benchmark uses 600 seconds per optimisation stage. The component ablation uses 180 seconds per stage and is analysed separately. These are computational allowances, distinct from the evacuation departure window. Search completion is assessed from incumbent, bound and tolerance records.

Direct and decomposed runs use the same realised instances, formulation, solver version, thread setting and stopping tolerances. Their stage timers include model setup, and the proposed procedures include their bounded warm-start work in reported runtime. Instance generation, final assignment recovery and validation contribute additional overhead. The allowances do not impose one common end-to-end wall-clock ceiling on policies with different numbers of objectives.

## Fixed longer-budget family

The follow-up uses concentrated demand, moderate scarcity and no capacity loss on Anaheim and Winnipeg, with all three existing demand draws and both direct MILP and strengthened Benders. This defines six instances and twelve method-instance pairs.

After the corresponding 600-second runs finish, the follow-up is triggered if any of these twelve runs lacks two-stage certification. Every pair then receives a single 3,600-second-per-stage run. Initialisation and the frozen scientific implementation are unchanged. Short-run incumbents are not selectively transferred. The original 600-second records remain the primary benchmark, and the longer-budget comparison is reported separately.

The maximum additional stage allowance is 24 hours across the twelve two-stage runs, with bounded warm-start and setup overhead. No repeated extension is made until certification. If all twelve short-budget runs are certified, the trigger is false and no longer-budget search is required.

## Interpretation

All selected runs remain in the comparison, including cases already certified at 600 seconds. Primary bounds describe uncertainty in the optimal floor. Secondary bounds are conditional on the transferred floor. Different floors can define different secondary feasible sets. Runtime at the time limit is a censored observation, not a measured time to optimality.

Intermediate callback points describe progress within a stage. They are not completed two-stage runs under new time budgets. This protocol assesses the central algorithm comparison. Other sensitivity and policy results retain their own bounds and scope.

The recorded trigger and completed outputs are in `audit/time_budget_gate_v1/decision.json` and `audit/time_budget_summary_v1/`. The rule was fixed after the core batch and before completion of the direct-comparison batch.
