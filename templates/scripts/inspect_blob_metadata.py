"""M0 - Blob metadata discovery (read-only, no LLM).

Reads the metadata key-values off every blob in the container and reports what
the service-function plan (../service_function_plan.md) needs before M1:

  - coverage: how many docs actually carry each metadata key (populated vs blank)
  - distinct values of Services_Function_Capabilities AND Platform, with counts
  - multi-tag distribution (how many docs carry 1, 2, 3+ scope tags)
  - IsDeleted count
  - list-serialization format sanity check (expected: JSON arrays)
  - a few sample rows

Nothing is modified. Metadata is fetched in ONE listing pass
(list_blobs(include=['metadata'])) - no per-blob calls, fast at 2k+ docs.

Usage (from the cosmos-rag folder):
    ..\\.venv\\Scripts\\python.exe -m scripts.inspect_blob_metadata
    ..\\.venv\\Scripts\\python.exe -m scripts.inspect_blob_metadata --container my-container
    ..\\.venv\\Scripts\\python.exe -m scripts.inspect_blob_metadata --json out.json
    ..\\.venv\\Scripts\\python.exe -m scripts.inspect_blob_metadata --samples 5
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from app.config import get_settings
from app.services.storage import _blob_service, _is_supported

# Metadata keys the plan cares about. Case-insensitive lookup is used, so the
# exact casing here only affects display.
SCOPE_LIST_KEYS = ["Services_Function_Capabilities", "Platform"]
OTHER_LIST_KEYS = ["Industry_Sector_Market", "Keywords"]
TEXT_KEYS = ["Short_Description", "Name", "Title", "webUrl", "FileDownloadUrl",
             "ModifiedTime", "IsDeleted", "Accelerator_Folder"]


def _arg(name: str, default=None):
    argv = sys.argv[1:]
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else default
    return default


def _ci_get(md: dict, key: str) -> str | None:
    """Case-insensitive metadata lookup - Azure may normalise key casing."""
    if key in md:
        return md[key]
    low = key.lower()
    for k, v in md.items():
        if k.lower() == low:
            return v
    return None


def parse_list_value(raw: str | None) -> tuple[list[str], str]:
    """Parse a blob-metadata list field. Returns (values, format_seen).

    Expected: JSON array of strings. Falls back to comma-split, then single
    value, so the discovery never crashes on an unexpected row. Empty strings
    are dropped (real data had ['']).
    """
    if raw is None:
        return [], "missing"
    s = raw.strip()
    if not s:
        return [], "blank"

    # JSON array (the format seen in sample metadata)
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                vals = [str(x).strip() for x in parsed if str(x).strip()]
                return vals, "json"
        except json.JSONDecodeError:
            pass

    # Fallbacks - recorded so the report flags any non-JSON rows
    if "," in s:
        return [v.strip() for v in s.split(",") if v.strip()], "comma"
    return [s], "scalar"


def main() -> int:
    settings = get_settings()
    if not settings.blob_mode:
        print("Azure Blob Storage is not configured (AZURE_STORAGE_CONNECTION_STRING).")
        return 1

    container = _arg("--container") or settings.azure_storage_container_name
    if not container:
        print("No container. Pass --container or set AZURE_STORAGE_CONTAINER_NAME in .env.")
        return 1
    n_samples = int(_arg("--samples", "3"))
    json_out = _arg("--json")

    service = _blob_service()
    cc = service.get_container_client(container)

    print(f"container: {container}")
    print(f"account  : {service.account_name}\n")
    print("listing blobs with metadata (one pass) ...")

    total = 0
    supported = 0
    key_present = Counter()      # metadata key -> docs where present & non-blank
    key_seen = Counter()         # metadata key -> docs where key exists at all
    scope_value_counts = {k: Counter() for k in SCOPE_LIST_KEYS}
    combined_scope_counts = Counter()   # union(Platform, SF) lowercased -> docs
    scope_tag_cardinality = Counter()   # how many combined scope tags per doc
    fmt_counts = Counter()              # serialization format seen across list fields
    industry_counts = Counter()
    deleted = 0
    no_scope_tags = 0
    samples = []

    for blob in cc.list_blobs(include=["metadata"]):
        name = blob.name
        if not _is_supported(name):
            continue
        total += 1
        md = blob.metadata or {}

        # coverage
        for key in SCOPE_LIST_KEYS + OTHER_LIST_KEYS + TEXT_KEYS:
            raw = _ci_get(md, key)
            if raw is not None:
                key_seen[key] += 1
                if raw.strip() and raw.strip() not in ('[""]', "[]"):
                    key_present[key] += 1

        # scope fields
        combined: set[str] = set()
        for key in SCOPE_LIST_KEYS:
            vals, fmt = parse_list_value(_ci_get(md, key))
            fmt_counts[fmt] += 1
            for v in vals:
                scope_value_counts[key][v] += 1
                combined.add(v.lower())
        for v in combined:
            combined_scope_counts[v] += 1
        scope_tag_cardinality[len(combined)] += 1
        if not combined:
            no_scope_tags += 1

        # other lists
        ind_vals, ind_fmt = parse_list_value(_ci_get(md, "Industry_Sector_Market"))
        fmt_counts[ind_fmt] += 1
        for v in ind_vals:
            industry_counts[v] += 1

        # IsDeleted
        isdel = (_ci_get(md, "IsDeleted") or "").strip().lower()
        if isdel in ("true", "1", "yes"):
            deleted += 1

        if len(samples) < n_samples:
            samples.append({
                "name": name,
                "title": _ci_get(md, "Title"),
                "service_functions": parse_list_value(_ci_get(md, "Services_Function_Capabilities"))[0],
                "platform": parse_list_value(_ci_get(md, "Platform"))[0],
                "keywords": parse_list_value(_ci_get(md, "Keywords"))[0][:6],
                "webUrl": _ci_get(md, "webUrl"),
                "isDeleted": _ci_get(md, "IsDeleted"),
            })
        supported += 1

    if total == 0:
        print("No supported documents found in the container.")
        return 1

    def pct(n: int) -> str:
        return f"{n}/{total} ({100*n//total}%)"

    print(f"\n{'='*70}")
    print(f"SUPPORTED DOCUMENTS: {total}")
    print(f"{'='*70}")

    print("\n-- Metadata coverage (populated / total) --")
    for key in SCOPE_LIST_KEYS + OTHER_LIST_KEYS + TEXT_KEYS:
        print(f"  {key:32s} {pct(key_present[key])}")

    print("\n-- List serialization format seen --")
    for fmt, n in fmt_counts.most_common():
        flag = "  <- unexpected, inspect" if fmt in ("comma", "scalar") else ""
        print(f"  {fmt:10s} {n} field-values{flag}")

    for key in SCOPE_LIST_KEYS:
        vc = scope_value_counts[key]
        print(f"\n-- {key} - {len(vc)} distinct values --")
        for val, n in vc.most_common():
            print(f"  {n:5d}  {val}")

    print(f"\n-- Combined scope tags (Platform + Services_Function, lowercased) - "
          f"{len(combined_scope_counts)} distinct --")
    for val, n in combined_scope_counts.most_common():
        print(f"  {n:5d}  {val}")

    print("\n-- Scope tags per document --")
    for k in sorted(scope_tag_cardinality):
        label = "NO scope tags" if k == 0 else f"{k} tag(s)"
        print(f"  {label:15s} {scope_tag_cardinality[k]} docs")

    cm = combined_scope_counts.get("clients and markets", 0)
    print(f"\n-- 'Clients and Markets' track --")
    print(f"  docs tagged 'clients and markets': {cm}"
          + ("" if cm else "   [!] ZERO - dual-track second search would be empty!"))

    print(f"\n-- Industry_Sector_Market - {len(industry_counts)} distinct --")
    for val, n in industry_counts.most_common(15):
        print(f"  {n:5d}  {val}")

    print(f"\n-- Hygiene --")
    print(f"  IsDeleted=true          : {deleted}")
    print(f"  docs with NO scope tags : {no_scope_tags}"
          + ("   [!] need a fallback rule for these" if no_scope_tags else ""))

    print(f"\n-- Sample rows --")
    for s in samples:
        print(f"  * {s['name']}")
        print(f"      title   : {s['title']}")
        print(f"      SF      : {s['service_functions']}")
        print(f"      platform: {s['platform']}")
        print(f"      keywords: {s['keywords']}")
        print(f"      webUrl  : {(s['webUrl'] or '')[:70]}")

    if json_out:
        report = {
            "container": container,
            "total_supported": total,
            "coverage": {k: key_present[k] for k in key_present},
            "formats": dict(fmt_counts),
            "service_function_values": dict(scope_value_counts["Services_Function_Capabilities"]),
            "platform_values": dict(scope_value_counts["Platform"]),
            "combined_scope_values": dict(combined_scope_counts),
            "scope_tag_cardinality": dict(scope_tag_cardinality),
            "industry_values": dict(industry_counts),
            "is_deleted": deleted,
            "no_scope_tags": no_scope_tags,
            "samples": samples,
        }
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\nJSON report written: {json_out}")

    print(f"\n{'='*70}")
    print("NEXT: use the distinct scope values to populate the UI dropdown, "
          "the coverage % to set the untagged-doc fallback rule, and confirm "
          "the format is 'json' before M1 parser is finalised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
