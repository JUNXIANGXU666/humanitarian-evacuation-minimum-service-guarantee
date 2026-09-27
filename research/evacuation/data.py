from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import networkx as nx
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GROUPS = ["General", "Older without specialised transport", "Specialised assistance"]


@dataclass(frozen=True)
class Settings:
    network: str
    pattern: str = "concentrated"
    scarcity: float = 0.8
    loss: float = 0.0
    replication: int = 0
    seed: int = 20260925
    k: int = 5
    detour: float = 1.5
    delta: float = 15.0
    window: float = 90.0
    concentration: float | None = None
    assisted_share: float | None = None
    assistance: float | None = None
    partition: str = "three"
    demand_scale: float = 1.0


@dataclass
class Instance:
    settings: dict
    origins: list[int]
    shelters: list[int]
    groups: list[str]
    demand: list[list[float]]
    total_demand: float
    arcs: list[dict]
    paths: list[dict]
    departures: int
    horizon: int
    capacities: list[float]
    shelter_capacity: list[float]
    response: list[float]
    assistance_weights: list[list[float]]
    assistance_capacity: list[list[float]]
    fairness_groups: list[list[int]]
    far_origins: list[int]
    loss_arcs: list[int]
    declared_nodes: int
    active_nodes: int
    source_hashes: dict

    def to_dict(self):
        return asdict(self)

    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"Refusing to overwrite different instance {path}")
        path.write_text(text, encoding="utf-8")

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@lru_cache(maxsize=4)
def load_network(code):
    if code == "ND":
        source = RAW / "ND.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        data["production"] = {int(k): v for k, v in data["production"].items()}
        data["attraction"] = {int(k): v for k, v in data["attraction"].items()}
        data["source_hashes"] = {source.name: sha256(source)}
        return data
    prefix = {"SF": "SiouxFalls", "Anaheim": "Anaheim", "Winnipeg": "Winnipeg"}[code]
    netfile, tripfile = RAW / f"{prefix}_net.tntp", RAW / f"{prefix}_trips.tntp"
    meta, arcs = {}, []
    for line in netfile.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*<([^>]+)>\s*(\d+)", line)
        if match:
            meta[match[1]] = int(match[2])
        elif re.match(r"\s*\d+\s", line):
            values = line.split(";")[0].split()
            arcs.append({"tail": int(values[0]), "head": int(values[1]),
                         "source_capacity": float(values[2]), "time": float(values[4])})
    if len(arcs) != meta["NUMBER OF LINKS"]:
        raise ValueError("TNTP link count mismatch")
    production, attraction, origin = {}, {}, None
    for line in tripfile.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*Origin\s+(\d+)", line)
        if match:
            origin = int(match[1])
        elif origin is not None:
            for dest, volume in re.findall(r"(\d+)\s*:\s*([\d.eE+\-]+)", line):
                production[origin] = production.get(origin, 0.0) + float(volume)
                attraction[int(dest)] = attraction.get(int(dest), 0.0) + float(volume)
    if not production or any(a["time"] <= 0 or a["source_capacity"] <= 0 for a in arcs):
        raise ValueError("Network needs positive capacities, times and trip productions")
    # A common scale preserves capacity ratios without claiming vehicle-to-person calibration.
    scale = 1000.0 / float(np.median([a["source_capacity"] for a in arcs]))
    for a in arcs:
        a["capacity"] = a["source_capacity"] * scale
    return {"arcs": arcs, "production": production, "attraction": attraction,
            "first_thru": meta["FIRST THRU NODE"], "declared_nodes": meta["NUMBER OF NODES"],
            "source_hashes": {f.name: sha256(f) for f in (netfile, tripfile)}}


def _graph(data, endpoints=None):
    graph = nx.DiGraph()
    for i, a in enumerate(data["arcs"]):
        if graph.has_edge(a["tail"], a["head"]):
            raise ValueError("Parallel arcs require an expanded graph")
        graph.add_edge(a["tail"], a["head"], weight=a["time"], arc=i)
    if endpoints is not None:
        graph.remove_nodes_from([n for n in graph if n < data["first_thru"] and n not in endpoints])
    return graph


@lru_cache(maxsize=64)
def topology(code, k, detour, delta):
    data = load_network(code)
    if code == "ND":
        origins, shelters = [1, 4, 9, 12], [2, 3]
    else:
        count_o, count_s = (4, 2) if code == "SF" else (10, 4)
        graph = _graph(data)
        origins = [n for n in sorted(data["production"], key=lambda n: (-data["production"][n], n))
                   if n in graph][:count_o]
        shelters = []
        for n in sorted(data["attraction"], key=lambda n: (-data["attraction"][n], n)):
            if n in origins or n not in graph:
                continue
            if all(nx.has_path(_graph(data, {o, n}), o, n) for o in origins):
                shelters.append(n)
            if len(shelters) == count_s:
                break
        if len(shelters) != count_s:
            raise ValueError("Insufficient reachable shelter candidates")
    paths, distances = [], {o: math.inf for o in origins}
    for o in origins:
        for s in shelters:
            graph = _graph(data, {o, s})
            for j, nodes in enumerate(nx.shortest_simple_paths(graph, o, s, weight="weight")):
                edges = [graph[u][v]["arc"] for u, v in zip(nodes[:-1], nodes[1:])]
                travel = sum(data["arcs"][a]["time"] for a in edges)
                if j == 0:
                    shortest = travel
                    distances[o] = min(distances[o], travel)
                if j >= k or travel > detour * shortest + 1e-9:
                    break
                offsets, elapsed = [], 0.0
                for a in edges:
                    end = elapsed + data["arcs"][a]["time"]
                    for q in range(int(math.floor(elapsed / delta)), int(math.ceil((end - 1e-10) / delta))):
                        if max(q * delta, elapsed) < min((q + 1) * delta, end) - 1e-10:
                            offsets.append([a, q])
                    elapsed = end
                paths.append({"origin": origins.index(o), "shelter": shelters.index(s),
                              "nodes": nodes, "arcs": edges, "time": travel, "offsets": offsets})
    far = sorted(origins, key=lambda o: (-distances[o], o))[:math.ceil(len(origins) / 2)]
    used = {a for p in paths for a in p["arcs"]}
    graph = nx.DiGraph()
    for a in sorted(used):
        arc = data["arcs"][a]
        graph.add_edge(arc["tail"], arc["head"], weight=arc["time"], arc=a)
    between = nx.edge_betweenness_centrality(graph, weight="weight")
    ranked = sorted(between, key=lambda e: (-between[e], graph.edges[e]["arc"]))
    loss_arcs = [graph.edges[e]["arc"] for e in ranked[:max(1, math.ceil(0.05 * len(used)))]]
    return origins, shelters, paths, far, loss_arcs


def build_instance(settings: Settings):
    data = load_network(settings.network)
    origins, shelters, paths, far, loss_arcs = topology(settings.network, settings.k, settings.detour, settings.delta)
    departures = int(round(settings.window / settings.delta))
    if departures < 1 or abs(departures * settings.delta - settings.window) > 1e-9:
        raise ValueError("Departure window must be a positive multiple of the time step")
    capacities = np.asarray([a["capacity"] for a in data["arcs"]], dtype=float)
    out_capacity = sum(a["capacity"] for a in data["arcs"] if a["tail"] in origins)
    total = settings.demand_scale * 6.0 * out_capacity
    weights = np.asarray([data["production"][o] for o in origins], dtype=float)
    if settings.replication:
        network_id = ["ND", "SF", "Anaheim", "Winnipeg"].index(settings.network)
        rng = np.random.default_rng(np.random.SeedSequence([settings.seed, network_id, settings.replication]))
        weights *= rng.uniform(0.8, 1.2, len(origins))
    weights /= weights.sum()
    shares = []
    for o in origins:
        if settings.concentration is not None:
            q = settings.concentration
            if not 0 <= q <= 1.8:
                raise ValueError("Concentration must lie in [0,1.8]")
            target = np.array([0.5, 0.3, 0.2]) if o in far else np.array([0.8, 0.15, 0.05])
            share = np.array([0.7, 0.2, 0.1]) * (1 - q) + target * q
        else:
            share = ([0.7, 0.2, 0.1] if settings.pattern == "balanced" else
                     ([0.5, 0.3, 0.2] if o in far else [0.8, 0.15, 0.05]))
        if settings.assisted_share is not None:
            if not 0 < settings.assisted_share < 0.8:
                raise ValueError("Assisted share must lie in (0,0.8)")
            share = [0.8 - settings.assisted_share, 0.2, settings.assisted_share]
        shares.append(share)
    demand = weights[:, None] * np.asarray(shares)
    groups = GROUPS.copy()
    assistance_weights = np.array([[0.0, 1.0, 2.0], [0.0, 0.0, 1.0]])
    fairness_groups = [[0], [1], [2]]
    if settings.partition in ("six", "three_fine", "two_fine", "rare"):
        if settings.partition == "rare":
            splits = np.array([[0.98, 0.02] for o in origins])
        else:
            splits = np.array([[0.25, 0.75] if o in far else [0.75, 0.25] for o in origins])
        demand = np.stack([demand[:, g] * splits[:, h] for g in range(3) for h in range(2)], axis=1)
        groups = [f"{name} {j + 1}" for name in GROUPS for j in range(2)]
        assistance_weights = np.array([[0.0, 0.0, 0.5, 1.5, 1.5, 2.5],
                                       [0.0, 0.0, 0.0, 0.0, 0.75, 1.25]])
        fairness_groups = ([[0, 1], [2, 3], [4, 5]] if settings.partition == "three_fine" else
                           ([[0, 1], [2, 3, 4, 5]] if settings.partition == "two_fine" else [[g] for g in range(6)]))
    capacities *= settings.scarcity / total
    capacities[loss_arcs] *= 1.0 - settings.loss
    response_rate = max(1, math.floor(settings.scarcity * len(origins)))
    response = [math.floor(response_rate * (t + 1) * settings.delta / 15.0 + 1e-9)
                - math.floor(response_rate * t * settings.delta / 15.0 + 1e-9) for t in range(departures)]
    assistance_capacity = []
    if settings.assistance is not None:
        # Fixed supply across concentration and group-definition tests uses the same base needs mix.
        reference = np.array([0.7, 0.2, 0.1])
        requirements = np.array([[0.0, 1.0, 2.0], [0.0, 0.0, 1.0]]) @ reference
        assistance_capacity = [[settings.assistance * value * slots / (6.0 * response_rate)
                                for slots in response] for value in requirements]
    horizon = departures + math.ceil(max(p["time"] for p in paths) / settings.delta)
    return Instance(asdict(settings), origins, shelters, groups, demand.tolist(), total,
                    data["arcs"], paths, departures, horizon, capacities.tolist(),
                    [settings.scarcity / len(shelters)] * len(shelters), response,
                    assistance_weights.tolist(), assistance_capacity, fairness_groups, far, loss_arcs,
                    data["declared_nodes"], len({a[k] for a in data["arcs"] for k in ("tail", "head")}),
                    data["source_hashes"])
