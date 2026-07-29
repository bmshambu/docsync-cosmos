"""Interactive knowledge-graph HTML generation.

Wraps the D3 graph generator bundled in app/services/graph_html_generator.py.
Path constants in that module are overridden at call time so the generator
works with our configured data directory.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_GENERATOR_PATH = Path(__file__).with_name("graph_html_generator.py")


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("_rfp_graph_html_gen", _GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load graph HTML generator at {_GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_html(gen, graph_data, title, subtitle) -> str:
    return gen.HTML_TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        stats_nodes=graph_data["stats"]["nodes"],
        stats_edges=graph_data["stats"]["edges"],
        stats_comms=graph_data["stats"]["communities"],
        graph_data_json=json.dumps(graph_data, ensure_ascii=False, indent=None),
    )


def generate_graph_html(
    entities_file: Path,
    relationships_file: Path,
    community_map_file: Path,
    communities_dir: Path,
    out_file: Path,
    title: str = "RFP Knowledge Graph Explorer",
) -> Path:
    gen = _load_generator_module()

    # Redirect the generator's hard-coded paths at our configured data dir.
    gen.ENTITIES_FILE = entities_file
    gen.RELATIONS_FILE = relationships_file
    gen.COMMUNITY_FILE = community_map_file
    gen.COMMUNITIES_DIR = communities_dir

    entities, relationships, community_map = gen.load_data()
    graph_data = gen.build_graph_data(entities, relationships, community_map)

    html = _render_html(gen, graph_data, title, "GraphRAG Knowledge Graph")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    return out_file


def generate_scope_graph_html(
    store,
    scope_value: str | None,
    node_cap: int = 600,
    title: str = "RFP Knowledge Graph Explorer",
) -> str:
    """Render an interactive graph HTML for ONE scope, straight from the store
    (no temp files) — the whole-corpus graph (~22k nodes) freezes the browser,
    so the viz is always scoped and degree-capped.

    scope_value None/'' → the whole corpus (still degree-capped). Otherwise the
    entity set is filtered to the scope's documents. Community colouring uses the
    scope's own per-scope community graph (item #4) when it has been built, else
    the global 'default' communities as a fallback (so colours still appear).
    """
    from app.services.graph_store import DEFAULT_GRAPH_ID, scope_graph_id
    from app.services.scope_communities import _scoped_entities

    gen = _load_generator_module()

    entities = store.get_entities()
    scope_ids = store.scoped_doc_ids(any_value=scope_value) if scope_value else None
    if scope_ids is not None:
        entities = _scoped_entities(entities, scope_ids)

    kept_ids = {e.get("id") for e in entities}
    relationships = [
        r for r in store.get_relationships()
        if r.get("source") in kept_ids and r.get("target") in kept_ids
    ]

    # Prefer the scope's own community graph; fall back to global for colouring.
    gid = scope_graph_id(scope_value) if scope_value else DEFAULT_GRAPH_ID
    community_map = store.get_community_map(gid) if gid != DEFAULT_GRAPH_ID else {}
    used_gid = gid
    if not community_map.get("communities"):
        community_map = store.get_community_map()          # global default
        used_gid = DEFAULT_GRAPH_ID
    summaries = store.get_all_summaries(used_gid)

    graph_data = gen.build_graph_data(
        entities, relationships, community_map,
        summaries=summaries, node_cap=node_cap,
    )

    s = graph_data["stats"]
    scope_label = scope_value or "Whole corpus"
    subtitle = f"Scope: {scope_label} · {s['nodes']} entities · {s['edges']} relationships"
    if s.get("capped"):
        subtitle += f" (top {s['nodes']} of {s['total_nodes']} by connectivity)"
    if used_gid == DEFAULT_GRAPH_ID and scope_value:
        subtitle += " · global communities (build per-scope communities for tighter clusters)"

    return _render_html(gen, graph_data, title, subtitle)


def generate_entity_graph_html(
    store,
    entity_ids: list[str],
    node_cap: int = 150,
    title: str = "Answer entities",
) -> str:
    """Render a FOCUSED graph of specific entities (the ones an answer cited) plus
    their 1-hop neighbourhood, with the cited entities highlighted. Opened from
    the Query tab's clickable 'N entities' citation.

    Shows the requested entities + every entity one relationship away, so the
    cited entities are seen in context rather than as floating dots."""
    gen = _load_generator_module()

    want = {i for i in (entity_ids or []) if i}
    if not want:
        raise ValueError("No entity ids supplied.")

    # Edges touching the requested entities → collect their neighbours.
    rels = store.get_relationships_for(want)
    keep = set(want)
    for r in rels:
        s, t = r.get("source"), r.get("target")
        if s in want and t:
            keep.add(t)
        if t in want and s:
            keep.add(s)

    entities = [e for e in store.get_entities() if e.get("id") in keep]
    relationships = [
        r for r in rels
        if r.get("source") in keep and r.get("target") in keep
    ]

    community_map = store.get_community_map()          # global, for colouring
    summaries = store.get_all_summaries()

    graph_data = gen.build_graph_data(
        entities, relationships, community_map,
        summaries=summaries, node_cap=node_cap, matched_ids=want,
    )

    s = graph_data["stats"]
    n_matched = sum(1 for n in graph_data["nodes"] if n.get("matched"))
    subtitle = (f"{n_matched} answer entit{'y' if n_matched == 1 else 'ies'} "
                f"(white ring) + neighbours · {s['nodes']} nodes · {s['edges']} relationships")
    return _render_html(gen, graph_data, title, subtitle)
