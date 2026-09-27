# Humanitarian evacuation with a minimum service guarantee

Computational materials for **Protecting vulnerable populations in humanitarian evacuation logistics through a minimum service guarantee**.

The study allocates shared transport capacity and group-specific assistance through a deterministic lexicographic mixed-integer linear programme. It compares direct solution, Benders branch-and-cut and alternative allocation priorities on four benchmark networks.

## Start here

- [Complete research archive](evacuation-research-v1.0.zip): executable implementation, network inputs, all scenario plans, realised instances, solver logs, allocations and results.
- [Results and manuscript map](RESULTS.md): find the records behind each results section, table and figure.
- [Run index](RUN_INDEX.csv): one row per formal run, with exact instance, provenance, result and log paths inside the archive.
- [Realised demand draws](realised_demand_draws.csv): origin multipliers and demands for the twelve network-replication combinations used across paired comparisons.
- [Reproduction guide](REPRODUCE.md): environment, entry points and commands.

The archive contains **466 completed formal runs**, comprising 454 main-study runs and 12 longer-budget comparisons. Of these, 463 returned independently checked feasible allocations and three matched-coverage searches terminated without an incumbent. Certification and numerical-safeguard outcomes remain in the records. A completed run is not necessarily an optimal solution.

## Organisation

| Path inside the archive | Contents |
| --- | --- |
| `research/evacuation/` | Common instance generator, constraints, direct solver, decomposition, comparison policies and allocation validator |
| `research/plans/` | Eight complete experiment plans, including settings, replications, methods and time limits |
| `research/data/` | Network inputs and source attribution |
| `research/runs/` | Saved instances, provenance, full solver output, stage records, allocations and validation checks |
| `evidence/formal_v1/` | Six figures, numerical tables and their provenance |
| `audit/collected_formal_v1/` | Strict collection of all 454 main-study records |
| `audit/time_budget_summary_v1/` | Twelve longer-budget records and paired comparisons |
| `research/tests/` | Implementation checks, with retained results under `research/runs/` |

## Integrity and scope

Run `python verify_release.py` from the extracted archive to check file hashes and the linkage from all 466 records to the frozen scientific source and experiment plans. This check does not start an optimisation run or require CPLEX. The six scientific modules, all experiment settings, raw inputs, saved instances, numerical results, allocations and solver logs are unchanged from the recorded computation. Machine-specific executable paths have been made portable. Internal working notes are excluded, and the unchanged time-budget decision rule has a public description. Integrity references to these publication metadata files have been updated consistently. Every affected file has its original and released SHA256 recorded in `release_manifest.json`.

The reported bounds and gaps apply to the stated finite path and time representation and numerical tolerances. Later-stage bounds are conditional on the targets transferred from earlier stages. These records do not establish unrestricted-route or continuous-time optimality.

## Data and software

Sioux Falls, Anaheim and Winnipeg network and trip files originate from [Transportation Networks for Research](https://github.com/bstabler/TransportationNetworks). The ND network is an author-specified diagnostic instance. Evacuation demands, population shares and resource budgets are constructed planning scenarios. See `research/data/SOURCES.md` and the manuscript for the construction.

The recorded computations used Python 3.11.9 and IBM ILOG CPLEX 22.2.0.0 on Windows 11. CPLEX must be obtained and licensed separately. This repository does not distribute the solver, its licence or a Python runtime. Third-party network data retain their source terms.
