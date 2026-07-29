"""Application configuration.

All settings come from environment variables (or a local .env file).
Path helpers derive the working-directory layout from DATA_DIR so the rest of
the app never hard-codes folder locations — this is what lets us swap the
local filesystem for Azure Blob storage later without touching the pipeline.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM provider ────────────────────────────────────────────
    llm_provider: str = "google"           # "google" | "azure_openai"

    # ── Google AI ───────────────────────────────────────────────
    google_api_key: str = ""
    model_extract: str = "gemini-2.5-flash"
    model_summary: str = "gemini-2.5-flash"
    model_query: str = "gemini-2.5-flash"

    # ── Azure OpenAI (used when llm_provider = "azure_openai") ──
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""        # https://my-resource.openai.azure.com/
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-02-15-preview"
    # Reasoning models (GPT-5.x, o1/o3) spend completion tokens on hidden
    # reasoning BEFORE producing output. For mechanical tasks (extraction,
    # summaries) set this low/minimal so reasoning doesn't eat the whole
    # token budget (empty output → parse failures) and each call stays fast.
    # Values: "" (don't send — non-reasoning models like gpt-4o), "minimal",
    # "low", "medium", "high".
    azure_reasoning_effort: str = ""

    # ── Access (simple PoC login) ───────────────────────────────
    # Set APP_PASSWORD to require a shared password before using the app.
    # Empty = no login (local dev). Session lasts APP_SESSION_HOURS.
    app_password: str = ""
    app_session_hours: int = 12

    # ── Pipeline tuning ─────────────────────────────────────────
    max_llm_concurrency: int = 5
    chunk_size: int = 400
    chunk_overlap: int = 50
    max_extract_tokens: int = 16000   # raise if using reasoning models (o1, GPT-4.5, etc.)
    max_query_tokens: int = 4096      # raise for longer cross-corpus comparisons
    max_summary_tokens: int = 8192    # thinking models burn tokens before output — keep high

    # ── Retrieval caps (see README "How retrieve() works") ──────
    top_entities: int = 10            # matched entities kept after ranking
    top_communities: int = 3          # community summaries used for global queries
    top_chunks: int = 4               # source chunks cited per answer
    chunk_candidate_limit: int = 200  # backend candidate cap before ranking
    max_prompt_entities: int = 8      # entities that reach the LLM prompt
    max_prompt_relationships: int = 15  # relationships that reach the LLM prompt
    title_match_docs: int = 3         # max docs matched by filename-as-question
    title_match_threshold: float = 0.4  # min title-word overlap ratio to count as a match

    # ── Storage ─────────────────────────────────────────────────
    data_dir: Path = Path("data")

    # ── Graph persistence backend ("file" | "cosmos") ───────────
    # "file"   → today's JSON/md layout under DATA_DIR
    # "cosmos" → Azure Cosmos DB NoSQL (migration step 2)
    storage_backend: str = "file"

    # ── Azure Cosmos DB (used when storage_backend = "cosmos") ──
    cosmos_endpoint: str = ""        # https://<account>.documents.azure.com:443/
    cosmos_key: str = ""
    cosmos_database: str = "graphrag"

    # ── Azure Blob Storage (document source) ────────────────────
    # Set both to switch from local folder to blob container as input source
    azure_storage_connection_string: str = ""
    azure_storage_container_name: str = ""

    # ── Derived path helpers ────────────────────────────────────
    @property
    def extracted_text_dir(self) -> Path:
        return self.data_dir / "extracted_text"

    @property
    def chunks_dir(self) -> Path:
        return self.data_dir / "chunks"

    @property
    def graph_dir(self) -> Path:
        return self.data_dir / "graph"

    @property
    def communities_dir(self) -> Path:
        return self.graph_dir / "communities"

    @property
    def entities_file(self) -> Path:
        return self.graph_dir / "entities.json"

    @property
    def relationships_file(self) -> Path:
        return self.graph_dir / "relationships.json"

    @property
    def community_map_file(self) -> Path:
        return self.graph_dir / "community_map.json"

    @property
    def graph_stats_file(self) -> Path:
        return self.graph_dir / "graph_stats.json"

    @property
    def graph_html_file(self) -> Path:
        return self.graph_dir / "knowledge_graph.html"

    @property
    def blob_mode(self) -> bool:
        """True when Azure Blob Storage is configured as the document source.

        AZURE_STORAGE_CONTAINER_NAME is optional — it only sets the default
        container; the UI lists all containers and lets the user pick.
        """
        return bool(self.azure_storage_connection_string)

    @property
    def blob_cache_dir(self) -> Path:
        """Deprecated: blobs are streamed in-memory now (no local cache).
        Kept only so older calls resolve; nothing is written here."""
        return self.data_dir / "blob_cache"

    @property
    def api_key_set(self) -> bool:
        if self.llm_provider == "azure_openai":
            return bool(self.azure_openai_api_key and self.azure_openai_endpoint)
        return bool(self.google_api_key)

    @property
    def active_model_label(self) -> str:
        if self.llm_provider == "azure_openai":
            return f"azure · {self.azure_openai_deployment}"
        return self.model_extract

    def summary_ok(self, comm_id: str) -> bool:
        """True if a community has a real summary md file on disk.

        The summary_file pointers in community_map.json are wiped every time
        the graph is rebuilt, so the md files are the source of truth.
        """
        try:
            md = self.communities_dir / f"community_{int(comm_id):02d}.md"
        except (ValueError, TypeError):
            return False
        if not md.exists():
            return False
        try:
            text = md.read_text(encoding="utf-8").strip()
        except Exception:
            return False
        return len(text) >= 100 and "Summary Unavailable" not in text[:80]

    def entities_count_on_disk(self) -> int:
        """Return how many entities are in entities.json (0 if file missing)."""
        import json
        if not self.entities_file.exists():
            return 0
        try:
            return len(json.loads(self.entities_file.read_text(encoding="utf-8")))
        except Exception:
            return 0

    def ensure_dirs(self) -> None:
        """Create the working-directory tree if it does not exist.
        (No blob cache — blobs are streamed in-memory.)"""
        for d in (self.extracted_text_dir, self.chunks_dir,
                  self.graph_dir, self.communities_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
