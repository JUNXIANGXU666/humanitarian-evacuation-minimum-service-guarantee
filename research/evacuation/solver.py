from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import cplex
import numpy as np
from cplex.callbacks import LazyConstraintCallback, MIPInfoCallback, UserCutCallback

from .formulation import Formulation, Row, add_rows
from .validation import gap_closed, objective_gap, validate


def configure(model, time_limit, log=None, lp=False):
    model.set_log_stream(log)
    model.set_results_stream(log)
    model.set_warning_stream(log)
    model.set_error_stream(log)
    model.parameters.threads.set(1 if lp else 4)
    model.parameters.parallel.set(1)
    model.parameters.randomseed.set(20260925)
    model.parameters.timelimit.set(float(time_limit))
    model.parameters.simplex.tolerances.feasibility.set(1e-9)
    model.parameters.simplex.tolerances.optimality.set(1e-9)
    if lp:
        model.parameters.lpmethod.set(2)
        model.parameters.preprocessing.presolve.set(0)
    else:
        model.parameters.mip.tolerances.mipgap.set(1e-4)
        model.parameters.mip.tolerances.absmipgap.set(1e-9)
        model.parameters.mip.tolerances.integrality.set(1e-9)


class Separation:
    def __init__(self, formulation, stage, previous_cuts=()):
        self.f, self.stage = formulation, stage
        self.lock = threading.RLock()
        self.lp = formulation.create_recourse(stage)
        configure(self.lp, 30.0, lp=True)
        self.cuts = list(previous_cuts)
        self.counts = {"integer_calls": 0, "fractional_calls": 0, "feasibility_cuts": 0,
                       "optimality_cuts": 0, "lp_seconds": 0.0, "invalid_duals": 0,
                       "max_dual_residual": 0.0, "max_primal_dual_error": 0.0,
                       "max_cut_correction": 0.0}
        self.error = None
        self.last_values = None
        self.last_result = None

    def evaluate(self, v):
        v = np.asarray(v)
        if self.last_values is not None and np.array_equal(v, self.last_values):
            return self.last_result
        rhs = self.f.h + self.f.B @ v
        self.lp.linear_constraints.set_rhs([(i, float(value)) for i, value in enumerate(rhs)])
        started = time.perf_counter()
        self.lp.solve()
        self.counts["lp_seconds"] += time.perf_counter() - started
        status = self.lp.solution.get_status()
        if status == self.lp.solution.status.infeasible:
            dual, proof = self.lp.solution.advanced.dual_farkas()
            dual = np.asarray(list(dual))
            scale = max(1e-12, np.max(np.abs(dual)))
            dual /= scale
            residual = max(0.0, float(np.max(self.f.A.T @ dual)),
                           float(np.max(dual[~self.f.equalities], initial=0)))
            violation = float(dual @ rhs)
            if residual > 1e-7 or violation <= 1e-9:
                raise RuntimeError(f"Invalid Farkas ray: residual={residual}, violation={violation}")
            result = ("infeasible", dual, None, None)
        elif status == self.lp.solution.status.optimal:
            dual = np.asarray(self.lp.solution.get_dual_values())
            cost = np.zeros(self.f.nx) if self.stage == 1 else self.f.cost
            residual = max(0.0, float(np.max(self.f.A.T @ dual - cost)),
                           float(np.max(dual[~self.f.equalities], initial=0)))
            objective = self.lp.solution.get_objective_value()
            error = abs(objective - dual @ rhs)
            self.counts["max_primal_dual_error"] = max(self.counts["max_primal_dual_error"], error)
            if residual > 1e-7 or error > 1e-7 * max(1.0, abs(objective)):
                raise RuntimeError(f"Invalid subproblem dual: residual={residual}, error={error}")
            result = ("optimal", dual, objective, np.asarray(self.lp.solution.get_values()))
        else:
            raise RuntimeError(f"Unresolved recourse LP: {self.lp.solution.get_status_string()}")
        self.counts["max_dual_residual"] = max(self.counts["max_dual_residual"], residual)
        self.last_values, self.last_result = v.copy(), result
        return result

    def separate(self, v):
        state, dual, objective, x = self.evaluate(v)
        if state == "optimal" and (self.stage == 1 or v[self.f.eta] >= objective - 1e-8):
            return None
        dual = dual.copy()
        dual[~self.f.equalities] = np.minimum(dual[~self.f.equalities], 0.0)
        cost = self.f.cost if state == "optimal" else np.zeros(self.f.nx)
        correction = float(np.maximum(self.f.A.T @ dual - cost, 0.0) @ np.asarray(self.f.xmax))
        correction = float(np.nextafter(correction, np.inf))
        self.counts["max_cut_correction"] = max(self.counts["max_cut_correction"], correction)
        coefficients = np.asarray(self.f.B.T @ dual).ravel()
        constant = float(dual @ self.f.h) - correction
        if state == "optimal":
            coefficients[self.f.eta] -= 1.0
        scale = max(1e-12, np.max(np.abs(coefficients)), abs(constant))
        coefficients /= scale
        rhs = float(np.nextafter(-constant / scale + 1e-12, np.inf))
        indices = np.flatnonzero(coefficients != 0.0).tolist()
        row = Row(indices, coefficients[indices].tolist(), "L", rhs, state)
        violation = float(coefficients @ v - rhs)
        if violation <= 1e-9:
            raise RuntimeError("Recourse failed but separating cut is not violated")
        if state == "infeasible":
            self.cuts.append(row)
            self.counts["feasibility_cuts"] += 1
        else:
            self.counts["optimality_cuts"] += 1
        return row


class LazyCuts(LazyConstraintCallback):
    def __call__(self):
        engine = self.engine
        with engine.lock:
            engine.counts["integer_calls"] += 1
            try:
                row = engine.separate(np.asarray(self.get_values()[:engine.f.nv]))
                if row is not None:
                    self.add(constraint=cplex.SparsePair(row.indices, row.values), sense=row.sense, rhs=row.rhs)
            except Exception as exc:
                engine.error = repr(exc)
                self.abort()


class FractionalCuts(UserCutCallback):
    def __call__(self):
        if not self.is_after_cut_loop():
            return
        node = self.get_num_nodes()
        if node > 0 and node % 25:
            return
        with self.engine.lock:
            self.engine.counts["fractional_calls"] += 1
            try:
                row = self.engine.separate(np.asarray(self.get_values()[:self.engine.f.nv]))
                if row is not None:
                    self.add(cut=cplex.SparsePair(row.indices, row.values), sense=row.sense, rhs=row.rhs,
                             use=self.use_cut.purge)
            except Exception as exc:
                self.engine.error = repr(exc)
                self.abort()


class Progress(MIPInfoCallback):
    def __call__(self):
        elapsed = self.get_time() - self.started
        incumbent = self.get_incumbent_objective_value() if self.has_incumbent() else None
        bound = self.get_best_objective_value()
        with self.lock:
            if (not self.records or elapsed - self.records[-1]["seconds"] >= 30 or
                    incumbent != self.records[-1]["incumbent"]):
                self.records.append({"seconds": elapsed, "incumbent": incumbent, "bound": bound,
                                     "nodes": self.get_num_nodes()})


def primal_start(f, seconds=20):
    started = time.perf_counter()
    relaxed = f.create_direct(1, relax=True)
    configure(relaxed, seconds, lp=True)
    relaxed.solve()
    if not relaxed.solution.is_primal_feasible():
        relaxed.end()
        return None, time.perf_counter() - started
    values = np.asarray(relaxed.solution.get_values())
    y = values[:f.ny].reshape(f.np, f.nt)
    selected = np.zeros_like(y)
    for t in range(f.nt):
        order = np.argsort(-y[:, t], kind="stable")
        selected[order[:math.floor(f.instance.response[t] + 1e-9)], t] = 1.0
    relaxed.variables.set_lower_bounds([(i, float(value)) for i, value in enumerate(selected.ravel())])
    relaxed.variables.set_upper_bounds([(i, float(value)) for i, value in enumerate(selected.ravel())])
    relaxed.solve()
    values = np.asarray(relaxed.solution.get_values()) if relaxed.solution.is_primal_feasible() else None
    relaxed.end()
    return values, time.perf_counter() - started


def solve_stage(f, stage, algorithm, seconds, logfile, floor=None, initial=None, previous_cuts=()):
    benders = algorithm != "direct"
    strengthened = algorithm in ("benders_bounds", "benders_fractional", "benders_warm", "benders_flow")
    fractional = algorithm in ("benders_fractional", "benders_warm", "benders_flow")
    started = time.perf_counter()
    model = f.create_master(stage, floor, strengthened) if benders else f.create_direct(stage, floor)
    if algorithm == "benders_flow":
        f.add_aggregate_flow_relaxation(model)
    log = Path(logfile).open("w", encoding="utf-8")
    configure(model, seconds, log)
    engine = None
    if benders:
        engine = Separation(f, stage, previous_cuts)
        add_rows(model, list(previous_cuts))
        model.parameters.mip.strategy.search.set(1)
        callback = model.register_callback(LazyCuts)
        callback.engine = engine
        if fractional:
            callback = model.register_callback(FractionalCuts)
            callback.engine = engine
    if initial is not None:
        v = initial[:f.nv] if benders else initial
        if algorithm == "benders_flow":
            w = np.asarray(initial[f.nv:f.nv + f.nx]).reshape(f.np, f.ng, f.nt).sum(axis=1).ravel()
            v = np.concatenate((v, w))
        model.MIP_starts.add(cplex.SparsePair(list(range(len(v))), [float(a) for a in v]),
                             model.MIP_starts.effort_level.check_feasibility)
    trace = model.register_callback(Progress)
    trace.records = []
    trace.lock = threading.Lock()
    trace.started = model.get_time()
    model.solve()
    elapsed = time.perf_counter() - started
    record = {"stage": stage, "algorithm": algorithm, "seconds": elapsed,
              "limit_seconds": seconds, "status_code": model.solution.get_status(),
              "status": model.solution.get_status_string(), "nodes": model.solution.progress.get_num_nodes_processed(),
              "trace": trace.records, "cut_stats": engine.counts if engine else {},
              "callback_error": engine.error if engine else None, "incumbent": None,
              "bound": None, "relative_gap": None, "absolute_gap": None,
              "solver_relative_gap": None, "certified": False, "validation": None}
    values, x = None, None
    if model.solution.is_primal_feasible():
        raw = np.asarray(model.solution.get_values())
        values = raw[:f.nv]
        if benders:
            state, dual, cost, x = engine.evaluate(values)
            if state != "optimal":
                raise RuntimeError("Final master candidate has infeasible recourse")
        else:
            x = raw[f.nv:]
        validation = validate(f.instance, values[:f.ny], values[f.ny:f.ny + f.nb], x,
                              floor or 0.0, values[f.z])
        bound = float(model.solution.MIP.get_best_objective())
        value = float(model.solution.get_objective_value())
        true_value = float(validation["z"]) if stage == 1 else float(f.cost @ x)
        true_bound = -bound if stage == 1 else bound
        record.update({"incumbent": true_value,
                       "bound": true_bound,
                       **objective_gap(true_value, true_bound, stage == 1),
                       "solver_relative_gap": float(model.solution.MIP.get_mip_relative_gap()),
                       "validation": validation})
        record["certified"] = (gap_closed(record) and validation["passed"] and
                               record["callback_error"] is None)
        if not validation["passed"]:
            raise RuntimeError(f"Invalid returned plan: {validation['violations']}")
        if stage == 2 and abs(float(f.cost @ x) - value) > 1e-7 * max(1.0, abs(value)):
            raise RuntimeError("Master objective and recovered travel-time objective disagree")
        record["solution"] = {"y": values[:f.ny].tolist(), "b": values[f.ny:f.ny + f.nb].tolist(),
                              "x": x.tolist()}
    else:
        try:
            bound = float(model.solution.MIP.get_best_objective())
            record["bound"] = -bound if stage == 1 else bound
        except cplex.exceptions.CplexError:
            pass
    cuts = engine.cuts if engine else []
    if engine:
        engine.lp.end()
    model.end()
    log.close()
    full = None if values is None else np.concatenate((values, x))
    return record, full, cuts


def solve(instance, algorithm="direct", seconds=600, directory=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    f = Formulation(instance)
    instance.save(directory / "instance.json")
    initial, warm_seconds = (primal_start(f) if algorithm in ("benders_warm", "benders_flow") else (None, 0.0))
    first, values, cuts = solve_stage(f, 1, algorithm, seconds, directory / "stage1.log", initial=initial)
    result = {"instance_sha256": instance.digest(), "settings": instance.settings, "algorithm": algorithm,
              "solver": "IBM ILOG CPLEX 22.2.0.0", "threads": 4, "mip_gap": 1e-4,
              "normalised_flows": True, "warm_start_seconds": warm_seconds,
              "dimensions": {"binary": f.ny, "assignment": f.nx, "recourse_rows": len(f.rows)},
              "stage1": first, "stage2": None, "certified": False}
    write_json(directory / "stage1.json", result)
    if values is not None:
        floor = max(0.0, first["validation"]["z"] - 1e-8)
        values[f.z] = floor
        values[f.eta] = float(f.cost @ values[f.nv:])
        second, final, unused = solve_stage(f, 2, algorithm, seconds, directory / "stage2.log", floor,
                                            values, cuts)
        result["stage2"] = second
        result["transferred_floor"] = floor
        result["certified"] = first["certified"] and second["certified"]
        result["metrics"] = second["validation"] if second["validation"] else first["validation"]
        final_primary = float(result["metrics"]["z"])
        result["final_primary"] = {"incumbent": final_primary, "bound": first["bound"],
                                   **objective_gap(final_primary, first["bound"], True)}
        result["certified"] = bool(result["certified"] and gap_closed(result["final_primary"]))
    result["total_seconds"] = warm_seconds + first["seconds"] + (result["stage2"]["seconds"] if result["stage2"] else 0)
    write_json(directory / "result.json", result)
    return result


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False, default=_json_scalar), encoding="utf-8")
    temporary.replace(path)


def _json_scalar(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Unsupported JSON value {type(value).__name__}")
