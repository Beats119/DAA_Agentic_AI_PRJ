"""Assignment document ingestion (PDF / plain text)."""

from .pdf import IngestionError, ingest_assignment

__all__ = ["IngestionError", "ingest_assignment"]
