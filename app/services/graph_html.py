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

    html = gen.HTML_TEMPLATE.format(
        title=title,
        stats_nodes=graph_data["stats"]["nodes"],
        stats_edges=graph_data["stats"]["edges"],
        stats_comms=graph_data["stats"]["communities"],
        graph_data_json=json.dumps(graph_data, ensure_ascii=False, indent=None),
    )

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    return out_file
