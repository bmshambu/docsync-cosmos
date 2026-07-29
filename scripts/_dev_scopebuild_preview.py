"""Dev-only launcher to verify the per-scope community build UI (item #4)
without a live Cosmos connection or real LLM spend. Not shipped — safe to delete.

Seeds a FILE-backend corpus (registry across both fields + entities + chunks +
default graph) and stubs the LLM summariser with a deterministic local writer,
then serves the app so the Community tab's "Per-scope communities" card can be
driven end-to-end (plan table, build job, live progress, built-status refresh).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data_scopebuild"
os.environ["STORAGE_BACKEND"] = "file"
os.environ["DATA_DIR"] = str(DATA)
os.environ["APP_PASSWORD"] = ""

from app.config import get_settings  # noqa: E402
from app.services.graph_build import build_and_save  # noqa: E402
from app.services.graph_store import FileGraphStore  # noqa: E402

s = FileGraphStore(get_settings())
s.save_doc_registry([
    {"doc_id": "oracle-erp", "filename": "oracle-erp.docx", "platform": ["oracle"], "service_functions": ["technology"]},
    {"doc_id": "oracle-crm", "filename": "oracle-crm.docx", "platform": ["oracle"], "service_functions": ["consulting"]},
    {"doc_id": "sap-fin", "filename": "sap-fin.docx", "platform": ["sap"], "service_functions": ["consulting"]},
    {"doc_id": "adv-note", "filename": "adv-note.docx", "platform": [], "service_functions": ["advisory"]},
    {"doc_id": "cm-deck", "filename": "cm-deck.docx", "platform": [], "service_functions": ["clients and markets"]},
])
ents = [
    {"id": "e1", "name": "Oracle PPM", "type": "methodology", "source_docs": ["oracle-erp.docx"]},
    {"id": "e2", "name": "Oracle SIT", "type": "concept", "source_docs": ["oracle-erp.docx"]},
    {"id": "e3", "name": "Oracle UAT", "type": "concept", "source_docs": ["oracle-erp.docx"]},
    {"id": "e4", "name": "Oracle CRM", "type": "methodology", "source_docs": ["oracle-crm.docx"]},
    {"id": "e5", "name": "SAP Finance", "type": "methodology", "source_docs": ["sap-fin.docx"]},
    {"id": "e6", "name": "Advisory Framework", "type": "capability", "source_docs": ["adv-note.docx"]},
    {"id": "e7", "name": "Client Reference", "type": "credential", "source_docs": ["cm-deck.docx"]},
]
rels = [
    {"source": "e1", "target": "e2", "relation_type": "requires", "source_doc": "oracle-erp.docx"},
    {"source": "e2", "target": "e3", "relation_type": "requires", "source_doc": "oracle-erp.docx"},
]
s.save_extraction(ents, rels)
for did, fn in [("oracle-erp", "oracle-erp.docx"), ("oracle-crm", "oracle-crm.docx"),
                ("sap-fin", "sap-fin.docx"), ("adv-note", "adv-note.docx"), ("cm-deck", "cm-deck.docx")]:
    s.save_doc_chunks(did, [{"doc_id": did, "filename": fn, "text": f"{fn} content body", "page_start": 1}])
build_and_save(s)   # default (global) community graph so prereqs are ready

# Stub the LLM summariser used by the scope builder — deterministic, no network.
import asyncio  # noqa: E402
import app.services.scope_communities as scm  # noqa: E402


async def _fake_summarise(store, *, model, max_concurrency, max_communities,
                          cancel_event, on_progress, max_tokens, graph_id, **_):
    cmap = store.get_community_map(graph_id)
    ids = list(cmap.get("communities", {}))
    if max_communities:
        ids = ids[:max_communities]
    out = []
    for k, cid in enumerate(ids, 1):
        await asyncio.sleep(0.15)   # visible incremental progress in the UI
        store.save_community_summary(
            cid, f"# {graph_id} · community {cid}\n\n" + "Scoped summary body. " * 30, graph_id)
        if on_progress:
            on_progress(k, len(ids), cid, None)
        out.append({"comm_id": cid})
    return out, False


scm.summarise_corpus = _fake_summarise

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=8032)
