from __future__ import annotations

from dataclasses import dataclass

import cplex
import numpy as np
from scipy.sparse import coo_matrix

from .data import Instance


@dataclass
class Row:
    indices: list[int]
    values: list[float]
    sense: str
    rhs: float
    name: str


class Formulation:
    """Shared master variables and recourse system A x <=/= h + B v."""

    def __init__(self, instance: Instance):
        self.instance = instance
        self.d = np.asarray(instance.demand)
        self.no, self.ng = self.d.shape
        self.np, self.nt = len(instance.paths), instance.departures
        self.ny = self.np * self.nt
        self.nb = self.no * self.ng
        self.z, self.eta = self.ny + self.nb, self.ny + self.nb + 1
        self.nv = self.eta + 1
        self.xkeys = [(p, g, t) for p in range(self.np) for g in range(self.ng) for t in range(self.nt)]
        self.nx = len(self.xkeys)
        self.cost = np.asarray([instance.paths[p]["time"] / 60.0 for p, g, t in self.xkeys])
        self.rows, self.rhs_terms = [], []
        demand_rows = [[] for _ in range(self.nb)]
        arc_rows, shelter_rows, assistance_rows = {}, [[] for _ in instance.shelters], {}
        self.xmax = []
        for j, (p, g, t) in enumerate(self.xkeys):
            path = instance.paths[p]
            o, s = path["origin"], path["shelter"]
            demand_rows[o * self.ng + g].append(j)
            shelter_rows[s].append(j)
            for a, q in path["offsets"]:
                arc_rows.setdefault((a, t + q), []).append(j)
            maximum = min(self.d[o, g], instance.shelter_capacity[s],
                          min(instance.capacities[a] for a in path["arcs"]))
            self.xmax.append(maximum)
            for r, supplies in enumerate(instance.assistance_capacity):
                weight = instance.assistance_weights[r][g]
                if weight:
                    assistance_rows.setdefault((r, t), []).append((j, weight))
        for og, indices in enumerate(demand_rows):
            self.add_recourse(indices, [1.0] * len(indices), "E", 0.0, [(self.ny + og, 1.0)], f"demand_{og}")
        for (a, q), indices in sorted(arc_rows.items()):
            self.add_recourse(indices, [1.0] * len(indices), "L", instance.capacities[a], [], f"arc_{a}_{q}")
        for s, indices in enumerate(shelter_rows):
            self.add_recourse(indices, [1.0] * len(indices), "L", instance.shelter_capacity[s], [], f"shelter_{s}")
        for (r, t), terms in sorted(assistance_rows.items()):
            self.add_recourse([j for j, v in terms], [v for j, v in terms], "L",
                              instance.assistance_capacity[r][t], [], f"assist_{r}_{t}")
        for j, (p, g, t) in enumerate(self.xkeys):
            o = instance.paths[p]["origin"]
            self.add_recourse([j], [1.0], "L", 0.0, [(p * self.nt + t, self.d[o, g])], f"link_{p}_{g}_{t}")
        ri, ci, vals, bri, bci, bvals = [], [], [], [], [], []
        for r, row in enumerate(self.rows):
            ri.extend([r] * len(row.indices))
            ci.extend(row.indices)
            vals.extend(row.values)
            for j, value in self.rhs_terms[r]:
                bri.append(r)
                bci.append(j)
                bvals.append(value)
        self.A = coo_matrix((vals, (ri, ci)), shape=(len(self.rows), self.nx)).tocsr()
        self.B = coo_matrix((bvals, (bri, bci)), shape=(len(self.rows), self.nv)).tocsr()
        self.h = np.asarray([row.rhs for row in self.rows])
        self.equalities = np.asarray([row.sense == "E" for row in self.rows])

    def add_recourse(self, indices, values, sense, rhs, terms, name):
        self.rows.append(Row(indices, values, sense, float(rhs), name))
        self.rhs_terms.append(terms)

    def service(self, v):
        b = np.asarray(v[self.ny:self.ny + self.nb]).reshape(self.d.shape)
        return np.array([b[:, groups].sum() / self.d[:, groups].sum() for groups in self.instance.fairness_groups])

    def master_rows(self, strengthened=False):
        rows = []
        for t in range(self.nt):
            rows.append(Row([p * self.nt + t for p in range(self.np)], [1.0] * self.np,
                            "L", self.instance.response[t], f"response_{t}"))
        for k, groups in enumerate(self.instance.fairness_groups):
            inds = [self.ny + o * self.ng + g for o in range(self.no) for g in groups]
            total = self.d[:, groups].sum()
            rows.append(Row(inds + [self.z], [-1.0 / total] * len(inds) + [1.0], "L", 0.0, f"floor_{k}"))
        if strengthened:
            inds = list(range(self.ny, self.ny + self.nb))
            rows.append(Row(inds, [1.0] * self.nb, "L", sum(self.instance.shelter_capacity), "shelter_total"))
            for r, supplies in enumerate(self.instance.assistance_capacity):
                vals = [self.instance.assistance_weights[r][g] for o in range(self.no) for g in range(self.ng)]
                rows.append(Row(inds, vals, "L", sum(supplies), f"assistance_total_{r}"))
            for o in range(self.no):
                inds = [self.ny + o * self.ng + g for g in range(self.ng)]
                vals = [1.0] * self.ng
                for p, path in enumerate(self.instance.paths):
                    if path["origin"] != o:
                        continue
                    maximum = min(self.d[o].sum(), self.instance.shelter_capacity[path["shelter"]],
                                  min(self.instance.capacities[a] for a in path["arcs"]))
                    for t in range(self.nt):
                        inds.append(p * self.nt + t)
                        vals.append(-maximum)
                rows.append(Row(inds, vals, "L", 0.0, f"origin_bound_{o}"))
            inds, vals = [self.eta], [1.0]
            for o in range(self.no):
                shortest = min(p["time"] / 60.0 for p in self.instance.paths if p["origin"] == o)
                for g in range(self.ng):
                    inds.append(self.ny + o * self.ng + g)
                    vals.append(-shortest)
            rows.append(Row(inds, vals, "G", 0.0, "travel_lower_bound"))
        return rows

    def create_master(self, stage, floor=None, strengthened=False, relax=False):
        model = cplex.Cplex()
        objective = [0.0] * self.nv
        objective[self.z if stage == 1 else self.eta] = -1.0 if stage == 1 else 1.0
        ub = [1.0] * self.ny + self.d.ravel().tolist() + [1.0, cplex.infinity]
        model.variables.add(obj=objective, lb=[0.0] * self.nv, ub=ub,
                            types="C" * self.nv if relax else "B" * self.ny + "C" * (self.nb + 2))
        add_rows(model, self.master_rows(strengthened))
        if floor is not None:
            model.variables.set_lower_bounds(self.z, float(floor))
        return model

    def create_direct(self, stage, floor=None, relax=False):
        model = self.create_master(stage, floor, strengthened=False, relax=relax)
        model.variables.add(obj=[0.0] * self.nx, lb=[0.0] * self.nx, ub=[cplex.infinity] * self.nx)
        rows = []
        for row, terms in zip(self.rows, self.rhs_terms):
            rows.append(Row([self.nv + j for j in row.indices] + [j for j, v in terms],
                            row.values + [-v for j, v in terms], row.sense, row.rhs, row.name))
        rows.append(Row([self.eta] + list(range(self.nv, self.nv + self.nx)),
                        [1.0] + (-self.cost).tolist(), "E", 0.0, "travel_value"))
        add_rows(model, rows)
        if relax:
            model.set_problem_type(model.problem_type.LP)
        return model

    def add_aggregate_flow_relaxation(self, model):
        start = model.variables.get_num()
        model.variables.add(lb=[0.0] * self.ny, ub=[cplex.infinity] * self.ny, obj=[0.0] * self.ny)
        rows, arc_rows = [], {}
        for o in range(self.no):
            inds = [start + p * self.nt + t for p, path in enumerate(self.instance.paths)
                    if path["origin"] == o for t in range(self.nt)]
            rows.append(Row(inds + [self.ny + o * self.ng + g for g in range(self.ng)],
                            [1.0] * len(inds) + [-1.0] * self.ng, "E", 0.0, f"aggregate_demand_{o}"))
        for p, path in enumerate(self.instance.paths):
            maximum = min(self.d[path["origin"]].sum(), self.instance.shelter_capacity[path["shelter"]],
                          min(self.instance.capacities[a] for a in path["arcs"]))
            for t in range(self.nt):
                j = start + p * self.nt + t
                rows.append(Row([j, p * self.nt + t], [1.0, -maximum], "L", 0.0, f"aggregate_link_{p}_{t}"))
                for a, q in path["offsets"]:
                    arc_rows.setdefault((a, q + t), []).append(j)
        for (a, q), inds in sorted(arc_rows.items()):
            rows.append(Row(inds, [1.0] * len(inds), "L", self.instance.capacities[a], f"aggregate_arc_{a}_{q}"))
        for s in range(len(self.instance.shelters)):
            inds = [start + p * self.nt + t for p, path in enumerate(self.instance.paths)
                    if path["shelter"] == s for t in range(self.nt)]
            rows.append(Row(inds, [1.0] * len(inds), "L", self.instance.shelter_capacity[s], f"aggregate_shelter_{s}"))
        inds = [self.eta] + list(range(start, start + self.ny))
        vals = [1.0] + [-path["time"] / 60.0 for path in self.instance.paths for t in range(self.nt)]
        rows.append(Row(inds, vals, "G", 0.0, "aggregate_travel_bound"))
        add_rows(model, rows)
        return start

    def create_recourse(self, stage):
        model = cplex.Cplex()
        model.variables.add(obj=[0.0] * self.nx if stage == 1 else self.cost.tolist(),
                            lb=[0.0] * self.nx, ub=[cplex.infinity] * self.nx)
        add_rows(model, self.rows)
        return model


def add_rows(model, rows):
    if not rows:
        return
    model.linear_constraints.add(lin_expr=[cplex.SparsePair(r.indices, r.values) for r in rows],
                                 senses="".join(r.sense for r in rows), rhs=[r.rhs for r in rows])
