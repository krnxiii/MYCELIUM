"""MYCELIUM exception hierarchy."""


class MyceliumError(Exception):
    """Base exception for all MYCELIUM errors."""


class ConnectionError(MyceliumError):
    """Neo4j connection failure."""


class SchemaError(MyceliumError):
    """Schema initialization or verification failure."""


class EmbeddingError(MyceliumError):
    """Embedding generation failure."""


class ExtractionError(MyceliumError):
    """LLM extraction failure."""
