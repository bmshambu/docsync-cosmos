"""Knowledge-graph construction + Louvain community detection.

Ported from ``skills/rfp-data-prep/build_graph.py``. Same NetworkX graph and
``community_louvain.best_partition`` logic; paths are passed in and results are
returned (in addition to being written) so the LangGraph node can report them.
"""

from __future__ import annotations

import json
from pathlib import Path

import community as community_louvain  # python-louvain
import networkx as nx


def build_graph(entities: list[dict], relationships: list[dict]) -> nx.Graph:
    G = nx.Graph()
    for e in entities:
        G.add_node(
            e["id"],
            name=e.get("name", e["id"]),
            entity_type=e.get("type", "unknown"),
            aliases=e.get("aliases", []),
            source_docs=e.get("source_docs", []),
        )

    for r in relationships:
        src, tgt = r.get("source"), r.get("target")
        if not src or not tgt or not G.has_node(src) or not G.has_node(tgt):
            continue
        if G.has_edge(src, tgt):
            G[src][tgt]["weight"] += 1
            G[src][tgt]["relations"].append(r.get("relation_type", "related"))
        else:
            G.add_edge(
                src,
                tgt,
                weight=1,
                relations=[r.get("relation_type", "related")],
                source_doc=r.get("source_doc", ""),
                page=r.get("page"),
            )
    return G


def detect_communities(G: nx.Graph, resolution: float) -> dict:
    if G.number_of_nodes() == 0:
        return {}
    return community_louvain.best_partition(G, resolution=resolution, random_state=42)


def build_community_map(entities: list[dict], partition: dict, G: nx.Graph) -> dict:
    entity_lookup = {e["id"]: e for e in entities}
    communities: dict[str, dict] = {}

    for node_id, comm_id in partition.items():
        comm_key = str(comm_id)
        if comm_key not in communities:
            communities[comm_key] = {
                "id": comm_id,
                "entities": [],
                "entity_types": {},
                "source_docs": set(),
                "internal_edges": 0,
                "summary": "",
            }
        entity = entity_lookup.get(
            node_id, {"id": node_id, "name": node_id, "type": "unknown", "source_docs": []}
        )
        communities[comm_key]["entities"].append(
            {"id": entity.get("id"), "name": entity.get("name"), "type": entity.get("type", "unknown")}
        )
        etype = entity.get("type", "unknown")
        communities[comm_key]["entity_types"][etype] = (
            communities[comm_key]["entity_types"].get(etype, 0) + 1
        )
        for doc in entity.get("source_docs", []):
            communities[comm_key]["source_docs"].add(doc)

    for u, v in G.edges():
        if partition.get(u) == partition.get(v):
            communities[str(partition[u])]["internal_edges"] += 1

    for c in communities.values():
        c["source_docs"] = sorted(c["source_docs"])

    return {
        "communities": communities,
        "node_to_community": {k: str(v) for k, v in partition.items()},
    }


def build_and_save(store, resolution: float = 1.0) -> dict:
    """Load entities/relationships from the GraphStore, build the graph, detect
    communities, persist community map + stats, and return the stats dict."""
    entities = store.get_entities()
    relationships = store.get_relationships()

    G = build_graph(entities, relationships)
    partition = detect_communities(G, resolution)
    community_map = build_community_map(entities, partition, G)

    store.save_community_map(community_map)

    stats = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "communities": len(community_map["communities"]),
        "resolution": resolution,
        "entities": len(entities),
        "relationships": len(relationships),
    }
    store.save_graph_stats(stats)
    return stats
