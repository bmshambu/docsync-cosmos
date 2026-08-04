"""Blob-metadata parsing + document registry records (M1 of the service-function plan).

Blob metadata values are plain strings; list fields (Services_Function_Capabilities,
Platform, Industry_Sector_Market, Keywords) are JSON arrays. This module turns a
blob's raw metadata dict into a normalized **document registry record** — the
single source of truth for scoped retrieval (Track A = Platform, Track B =
Services_Function 'Clients and Markets').

Scoping is done by JOIN (doc_id sets), never by re-tagging entities — so the
paid-for extraction is untouched and re-tagging later needs only a cheap re-sync.
"""

from __future__ import annotations

import json

CLIENTS_AND_MARKETS = "clients and markets"   # canonical lowercased Track-B tag


def parse_list_value(raw: str | None) -> list[str]:
    """Parse a blob-metadata list field (JSON array of strings).

    Drops empty strings (real data had ['']); falls back to comma/scalar so it
    never crashes on an unexpected row. Returns values as-stored (not lowered).
    """
    if raw is None:
        return []
    s = raw.strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    if "," in s:
        return [v.strip() for v in s.split(",") if v.strip()]
    return [s]


def _ci_get(md: dict, key: str) -> str | None:
    """Case-insensitive metadata lookup — Azure may normalise key casing."""
    if key in md:
        return md[key]
    low = key.lower()
    for k, v in md.items():
        if k.lower() == low:
            return v
    return None


def _is_true(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("true", "1", "yes")


def build_doc_record(doc_id: str, filename: str, md: dict) -> dict:
    """Build a registry record from a blob's raw metadata dict.

    Scope arrays (`platform`, `service_functions`) are LOWERCASED for
    case-insensitive matching; display/text fields keep original casing.
    """
    platform = [p.lower() for p in parse_list_value(_ci_get(md, "Platform"))]
    service_functions = [s.lower() for s in parse_list_value(_ci_get(md, "Services_Function_Capabilities"))]
    return {
        "doc_id": doc_id,
        "filename": filename,
        "platform": platform,                       # Track A filter
        "service_functions": service_functions,     # Track B filter
        "industry": parse_list_value(_ci_get(md, "Industry_Sector_Market")),
        "keywords": parse_list_value(_ci_get(md, "Keywords")),
        "title": _ci_get(md, "Title") or "",
        "short_description": _ci_get(md, "Short_Description") or "",
        "name": _ci_get(md, "Name") or filename,
        "web_url": _ci_get(md, "webUrl") or _ci_get(md, "FileDownloadUrl") or "",
        "modified_time": _ci_get(md, "ModifiedTime") or "",
        "is_deleted": _is_true(_ci_get(md, "IsDeleted")),
    }


def record_in_platform(rec: dict, platform: str) -> bool:
    return platform.strip().lower() in (rec.get("platform") or [])


def record_in_clients_and_markets(rec: dict) -> bool:
    return CLIENTS_AND_MARKETS in (rec.get("service_functions") or [])
