"""The shared paper record, and identifier and title normalization."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


@dataclass
class WorkRecord:
    """One paper from OpenAlex, flattened for storage."""

    openalex_id: str
    doi: str | None = None
    arxiv_id: str | None = None
    title: str | None = None
    year: int | None = None
    pub_date: str | None = None
    type: str | None = None
    venue: str | None = None
    venue_id: str | None = None
    venue_type: str | None = None
    authors: list[str] = field(default_factory=list)
    author_ids: list[str | None] = field(default_factory=list)  # OpenAlex IDs, in `authors` order
    abstract: str | None = None
    cited_by_count: int | None = None
    percentile: float | None = None
    referenced_ids: list[str] = field(default_factory=list)
    n_refs: int = 0
    topic: str | None = None  # OpenAlex primary topic, within subfield and field
    subfield: str | None = None
    field: str | None = None


def normalize_doi(doi: str) -> str:
    """Strip URL or `doi:` prefixes and lowercase the result."""
    return _DOI_PREFIX.sub("", doi.strip()).strip().lower()


def clean_arxiv_id(arxiv_id: str) -> str:
    """Lowercase an arXiv ID and remove any version."""
    text = re.sub(r"^arxiv:\s*", "", arxiv_id.strip(), flags=re.IGNORECASE)
    return _ARXIV_VERSION.sub("", text).lower()


def arxiv_doi(arxiv_id: str) -> str:
    """Return the `10.48550/arxiv.*` DOI, used to look up arXiv papers in OpenAlex."""
    return f"10.48550/arxiv.{clean_arxiv_id(arxiv_id)}"


def normalize_title(title: str) -> str:
    """Return a title form suitable for comparing two records."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
