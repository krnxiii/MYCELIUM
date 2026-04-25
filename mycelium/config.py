"""Configuration: env > defaults. Single source of truth."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env search chain (later overrides earlier):
#   1. ~/.mycelium/.env   — global user config
#   2. <project>/.env     — project root (works for editable install)
#   3. ./.env             — cwd override
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_CHAIN    = [
    Path.home() / ".mycelium" / ".env",
    _PROJECT_ROOT / ".env",
    ".env",
]


class Neo4jSettings(BaseModel):
    uri:                     str       = "bolt://localhost:7687"
    user:                    str       = "neo4j"
    password:                SecretStr = SecretStr("password")
    database:                str       = "neo4j"
    pool_size:               int       = 50
    pool_timeout:            float     = 30.0


class DecaySettings(BaseModel):
    base_rate:               float     = 0.008    # half-life ~90 days
    consolidation_factor:    float     = 0.3
    min_rate:                float     = 0.001
    evidence_boost:          float     = 0.1


class SemanticSettings(BaseModel):
    provider:                str       = "api"    # api | local | mock
    model_name:              str       = "BAAI/bge-m3"
    dimensions:              int       = 1024
    api_base_url:            str       = ""
    api_key:                 str       = ""
    max_tokens:              int       = 8192
    embed_entity_name:       bool      = True
    embed_entity_summary:    bool      = True
    embed_episode_content:   bool      = True
    embed_fact:              bool      = True
    reranker_model:          str       = "BAAI/bge-reranker-v2-m3"


class LLMSettings(BaseModel):
    provider:                str       = "cc-cli"  # cc-cli | api
    model:                   str       = "sonnet"
    max_retries:             int       = 3
    timeout:                 float     = 300.0
    deep_timeout:            float     = 600.0   # timeout for deep document extraction
    session_enabled:         bool      = True
    api_base:                str       = ""       # api: custom endpoint (Ollama, vLLM)
    api_key:                 str       = ""       # api: API key (Bearer token)


class SearchSettings(BaseModel):
    top_k:                   int       = 10
    min_score:               float     = 0.0      # filter results below this RRF score (0 = off)
    bfs_depth:               int       = 2
    rrf_k:                   int       = 60
    mmr_enabled:             bool      = False
    mmr_lambda:              float     = 0.7      # 1.0 = pure relevance, 0.0 = max diversity
    blend_enabled:           bool      = True     # position-aware blending (CF#12)
    reranker_chain:          list[str] = ["decay", "blend", "mmr"]
    # R2.1: cross-encoder reranking (DeepInfra bge-reranker)
    cross_encoder_enabled:   bool      = False    # opt-in (requires API)
    cross_encoder_top_n:     int       = 20       # max results to rerank
    # R2.2: node distance reranking (graph proximity to owner)
    node_distance_enabled:   bool      = False    # opt-in (requires owner neuron)
    node_distance_max_depth: int       = 5        # BFS max hops
    node_distance_weight:    float     = 0.15     # blend weight for distance boost


class DedupSettings(BaseModel):
    cosine_threshold:        float     = 0.95
    llm_threshold:           float     = 0.85     # grey zone: [llm_threshold, cosine_threshold)
    llm_enabled:             bool      = True
    llm_batch_size:          int       = 10       # max pairs per LLM call


class ContradictionSettings(BaseModel):
    enabled:                 bool      = True
    cosine_threshold:        float     = 0.65     # similarity zone for LLM check [0.65, 0.95)
    auto_expire_confidence:  float     = 0.9      # LLM confidence to auto-expire old
    llm_batch_size:          int       = 10


class InteractionSettings(BaseModel):
    level:                   str       = "balanced"    # silent | minimal | balanced | curious


class OwnerSettings(BaseModel):
    name:                    str       = ""    # empty = not set (DEV PATH)


class IngestionSettings(BaseModel):
    max_chunk_chars:         int       = 4000
    chunk_overlap:           int       = 500
    deep_threshold:          int       = 20000
    deep_extract_threshold:  int       = 6000     # chars: two-stage extraction (BL-15)
    gleaning_enabled:        bool      = True     # second pass to find missed facts (BL-16)
    gleaning_threshold:      int       = 8000     # chars: gleaning only above this (was 4000)
    max_parallel_chunks:     int       = 3
    batch_max_items:         int       = 50
    context_injection:       bool      = True     # R4.1: feed graph state into extraction prompt
    context_top_n:           int       = 10       # top neurons by weight to inject
    context_recent_n:        int       = 10       # recent neurons to inject
    cascade_invalidation:    bool      = False    # R5.2: mark derived synapses stale on merge


class CommunitySettings(BaseModel):
    min_community_size:      int       = 3
    resolution:              float     = 1.0      # Louvain: higher = more clusters
    max_communities:         int       = 50       # cap on LLM calls


class SummarySettings(BaseModel):
    min_facts:               int       = 2
    top_n:                   int       = 20


class TendSettings(BaseModel):
    """Maintenance toolkit (`mycelium tend`) — periodic graph upkeep."""
    staleness_hours:         int       = 24       # search falls back to on-read calc when older
    weak_threshold:          float     = 0.05     # decay_sweep marks below this as weak candidate
    sweep_batch_size:        int       = 1000     # nodes per UNWIND batch (avoid huge tx)
    zombie_age_hours:        int       = 24       # 'extracting' Signals older than this → failed
    vault_check_graph:       bool      = True     # cross-check vault ↔ graph (slower)


class RenderSettings(BaseModel):
    enabled:            bool = False
    host:               str  = "0.0.0.0"
    port:               int  = 9633


class MCPSettings(BaseModel):
    transport:          str  = "stdio"   # stdio | streamable-http
    host:               str  = "0.0.0.0"
    port:               int  = 9631
    auth_token:         str  = ""        # empty = no auth (local); set for HTTP


class TelegramSettings(BaseModel):
    bot_token:       str   = ""        # @BotFather token
    owner_chat_id:   int   = 0         # authorized user's chat_id
    mcp_url:         str   = "http://localhost:9631/mcp"
    mcp_auth_token:  str   = ""        # Bearer token for MCP HTTP (fallback: mcp.auth_token)
    debounce_sec:    float = 1.5       # text debounce window
    rate_limit:      int   = 30        # max messages per minute
    session_ttl:     int   = 14400    # agent session TTL in seconds (4h)
    # Voice STT
    stt_provider:    str   = "none"    # whisper-local | deepgram | none
    stt_api_key:     str   = ""        # Deepgram API key
    stt_whisper_url: str   = "http://whisper:8000"  # Whisper container URL
    stt_model:       str   = "medium"  # Whisper model name
    stt_language:    str   = "auto"    # auto | ru | en | etc


class ObsidianSettings(BaseModel):
    enabled:              bool  = True
    project_neurons:      bool  = True     # project neurons as .md files in vault/neurons/
    min_shared_neurons:   int   = 1
    max_related:          int   = 20
    include_expired:      bool  = False
    similarity_threshold: float = 0.75     # cosine threshold for file similarity links
    max_similar:          int   = 10       # max similar files per file


class VaultSettings(BaseModel):
    path:                    Path      = Field(
        default_factory=lambda: Path.home() / ".mycelium" / "vault",
    )

    @field_validator("path", mode="before")
    @classmethod
    def expand_home(cls, v: str | Path) -> Path:
        return Path(v).expanduser()


class LogSettings(BaseModel):
    level:                   str       = "INFO"
    format:                  str       = "auto"       # auto | json | console
    dir:                     Path      = Field(
        default_factory=lambda: Path.home() / ".mycelium" / "logs",
    )
    max_bytes:               int       = 10_485_760   # 10 MB
    backup_count:            int       = 3

    @field_validator("dir", mode="before")
    @classmethod
    def expand_home(cls, v: str | Path) -> Path:
        return Path(v).expanduser()


class Settings(BaseSettings):
    """MYCELIUM v2 settings. Priority: env > defaults."""

    model_config = SettingsConfigDict(
        env_prefix="MYCELIUM_",
        env_nested_delimiter="__",
        env_file=_ENV_CHAIN,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    neo4j:                   Neo4jSettings         = Neo4jSettings()
    decay:                   DecaySettings         = DecaySettings()
    semantic:                SemanticSettings      = SemanticSettings()
    llm:                     LLMSettings           = LLMSettings()
    search:                  SearchSettings        = SearchSettings()
    dedup:                   DedupSettings         = DedupSettings()
    contradiction:           ContradictionSettings = ContradictionSettings()
    ingestion:               IngestionSettings     = IngestionSettings()
    summary:                 SummarySettings       = SummarySettings()
    vault:                   VaultSettings         = VaultSettings()
    log:                     LogSettings           = LogSettings()
    community:               CommunitySettings     = CommunitySettings()
    render:                  RenderSettings        = RenderSettings()
    mcp:                     MCPSettings           = MCPSettings()
    owner:                   OwnerSettings         = OwnerSettings()
    interaction:             InteractionSettings   = InteractionSettings()
    obsidian:                ObsidianSettings      = ObsidianSettings()
    telegram:                TelegramSettings      = TelegramSettings()
    tend:                    TendSettings          = TendSettings()


def load_settings() -> Settings:
    """Load settings from env vars + .env file."""
    return Settings()
