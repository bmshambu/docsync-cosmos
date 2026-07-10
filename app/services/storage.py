"""Document source abstraction.

FolderSource reads from a local path.
AzureBlobSource lists blobs in a container — optionally restricted to selected
virtual folders (prefixes) — downloads them to a local cache dir, and returns
the local paths. The rest of the pipeline is unchanged.

The storage account can hold multiple containers (gps-proposals, rfp-docs, …);
the UI lists them and the user picks one per run. AZURE_STORAGE_CONTAINER_NAME
in .env is only the default selection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".ppt"}


def _is_supported(name: str) -> bool:
    return any(name.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)


def _blob_service():
    from azure.storage.blob import BlobServiceClient  # lazy import
    from app.config import get_settings
    settings = get_settings()
    if not settings.blob_mode:
        raise RuntimeError("Azure Blob Storage is not configured (AZURE_STORAGE_CONNECTION_STRING).")
    return BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)


class DocumentSource(Protocol):
    """A source of RFP documents that can be materialised as local files."""

    def list_documents(self) -> list[Path]:
        """Return local paths to every supported document in the source."""
        ...


class FolderSource:
    """Reads documents from a folder path on the server's filesystem."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def validate(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Folder does not exist: {self.path}")
        if not self.path.is_dir():
            raise NotADirectoryError(f"Not a folder: {self.path}")

    def list_documents(self) -> list[Path]:
        self.validate()
        return [
            p
            for p in sorted(self.path.glob("*"))
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]


class AzureBlobSource:
    """Download RFP blobs from an Azure Blob container to a local cache dir.

    ``prefixes`` restricts listing to the given virtual folders inside the
    container (e.g. ["Oracle", "Audit-Public"]). The empty-string prefix ""
    means files at the container ROOT only. No prefixes = whole container.

    Already-downloaded blobs are reused unless modified (ETag sidecar check).
    The cache is namespaced per container and mirrors the blob folder
    structure, so same-named files never collide.
    """

    def __init__(
        self,
        connection_string: str,
        container: str,
        prefixes: list[str] | None = None,
        local_cache: Path | None = None,
    ):
        self._conn_str = connection_string
        self._container = container
        self._prefixes = (
            [p.strip().strip("/") for p in prefixes] if prefixes is not None else None
        )
        base = local_cache or Path("data/blob_cache")
        self._cache_dir = base / container
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _container_client(self):
        from azure.storage.blob import BlobServiceClient
        service = BlobServiceClient.from_connection_string(self._conn_str)
        return service.get_container_client(self._container)

    def list_documents(self) -> list[Path]:
        cc = self._container_client()

        local_paths: list[Path] = []
        seen: set[str] = set()
        prefixes = self._prefixes if self._prefixes is not None else [None]

        for prefix in prefixes:
            starts_with = f"{prefix}/" if prefix else None
            for blob in cc.list_blobs(name_starts_with=starts_with):
                blob_name: str = blob.name
                if blob_name in seen or not _is_supported(blob_name):
                    continue
                # "" prefix = container root only — skip anything inside a folder
                if prefix == "" and "/" in blob_name:
                    continue
                seen.add(blob_name)

                local_file = self._cache_dir / blob_name
                local_file.parent.mkdir(parents=True, exist_ok=True)
                etag_file = local_file.with_suffix(local_file.suffix + ".etag")

                current_etag = blob.etag or ""
                cached_etag = (
                    etag_file.read_text(encoding="utf-8").strip()
                    if etag_file.exists() else ""
                )
                if not local_file.exists() or current_etag != cached_etag:
                    blob_client = cc.get_blob_client(blob_name)
                    with open(local_file, "wb") as f:
                        f.write(blob_client.download_blob().readall())
                    etag_file.write_text(current_etag, encoding="utf-8")

                local_paths.append(local_file)

        return sorted(local_paths)


# ── Blob helpers used by the API layer ────────────────────────────────────────

def list_blob_containers() -> list[str]:
    """List container names in the storage account (system $-containers excluded)."""
    service = _blob_service()
    return sorted(
        c.name for c in service.list_containers() if not c.name.startswith("$")
    )


def list_blob_folders(container: str | None = None) -> list[dict]:
    """List virtual folders in a container with supported-doc counts.

    Returns [{"path": "Oracle", "count": 12}, {"path": "", "count": 3}, …]
    where path "" is the container root. Only folders that directly contain at
    least one supported document are returned.
    """
    from app.config import get_settings
    settings = get_settings()
    container = container or settings.azure_storage_container_name
    if not container:
        return []

    service = _blob_service()
    cc = service.get_container_client(container)

    counts: dict[str, int] = {}
    for blob in cc.list_blobs():
        if not _is_supported(blob.name):
            continue
        folder = blob.name.rsplit("/", 1)[0] if "/" in blob.name else ""
        counts[folder] = counts.get(folder, 0) + 1

    return [{"path": k, "count": v} for k, v in sorted(counts.items())]


def _find_blob(filename: str, container: str | None = None) -> tuple[str, str] | None:
    """Resolve a bare filename to (container, full_blob_path).

    Documents are tracked pipeline-wide by basename; blobs may live in any
    container and folder. Searches the hinted/default container first, then
    every other container.
    """
    from app.config import get_settings
    settings = get_settings()
    service = _blob_service()

    hinted = container or settings.azure_storage_container_name
    candidates = [c for c in ([hinted] if hinted else []) if c]
    try:
        candidates += [c for c in list_blob_containers() if c not in candidates]
    except Exception:
        pass

    for cont in candidates:
        try:
            cc = service.get_container_client(cont)
            for blob in cc.list_blobs():
                if blob.name == filename or blob.name.endswith("/" + filename):
                    return cont, blob.name
        except Exception:
            continue
    return None


def get_blob_view_url(filename: str, expiry_hours: int = 2,
                      container: str | None = None) -> str | None:
    """Return a short-lived SAS URL for viewing/downloading a blob.

    Accepts a bare filename (resolved across containers/folders) or a full
    blob path. Returns None when blob mode is not configured.
    """
    from app.config import get_settings
    settings = get_settings()
    if not settings.blob_mode:
        return None

    found = _find_blob(filename, container=container)
    if not found:
        return None
    cont, blob_path = found

    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    service = _blob_service()
    sas = generate_blob_sas(
        account_name=service.account_name,
        container_name=cont,
        blob_name=blob_path,
        account_key=service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
    )
    return f"{service.url}{cont}/{blob_path}?{sas}"


def get_source(
    folder_path: str = "",
    prefixes: list[str] | None = None,
    container: str | None = None,
) -> DocumentSource:
    """Return the appropriate DocumentSource.

    Blob mode (connection string set): reads from ``container`` (or the .env
    default), optionally restricted to ``prefixes`` (virtual folders).
    Otherwise falls back to FolderSource(folder_path).
    """
    from app.config import get_settings
    settings = get_settings()
    if settings.blob_mode:
        cont = container or settings.azure_storage_container_name
        if not cont:
            raise ValueError("No blob container selected — pick one in the UI "
                             "or set AZURE_STORAGE_CONTAINER_NAME in .env.")
        return AzureBlobSource(
            connection_string=settings.azure_storage_connection_string,
            container=cont,
            prefixes=prefixes,
            local_cache=settings.blob_cache_dir,
        )
    return FolderSource(folder_path)
