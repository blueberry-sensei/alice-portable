"""Field descriptions of the vector indexes (backend neutral).

Source: the field definitions of the 4 elasticsearch_dsl Documents in core/storage/documents/.
Purpose: an embedded backend (LanceDB) creates its tables and FTS indexes from this; it is also the single authoritative list of each index's fields.
Vector dimensions are not declared here - they are inferred from the vector length of the first batch written (matching pgvector).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexSchema:
    """The field categories of one "index" (-> a LanceDB table)."""

    name: str
    vector_fields: tuple[str, ...] = ()
    text_fields: tuple[str, ...] = ()  # full-text (BM25) fields
    keyword_fields: tuple[str, ...] = ()  # exact-match strings
    array_fields: tuple[str, ...] = ()  # list<string>
    bool_fields: tuple[str, ...] = ()
    datetime_fields: tuple[str, ...] = ()  # always stored as ISO-8601 strings
    int_fields: tuple[str, ...] = ()

    def all_fields(self) -> tuple[str, ...]:
        return (
            self.vector_fields
            + self.text_fields
            + self.keyword_fields
            + self.array_fields
            + self.bool_fields
            + self.datetime_fields
            + self.int_fields
        )


INDEX_SCHEMAS: dict[str, IndexSchema] = {
    "entity_vectors": IndexSchema(
        name="entity_vectors",
        vector_fields=("vector",),
        text_fields=("name",),
        keyword_fields=("entity_id", "source_config_id", "type"),
        bool_fields=("is_delete",),
        datetime_fields=("created_time",),
    ),
    "event_vectors": IndexSchema(
        name="event_vectors",
        vector_fields=("title_vector", "content_vector"),
        text_fields=("title", "summary", "content"),
        keyword_fields=("event_id", "source_config_id", "source_type", "source_id", "category"),
        array_fields=("tags", "entity_ids"),
        bool_fields=("is_delete",),
        datetime_fields=("start_time", "end_time", "created_time"),
    ),
    "event_entity_vectors": IndexSchema(
        name="event_entity_vectors",
        vector_fields=("vector",),
        text_fields=("description",),
        keyword_fields=("event_id", "entity_id", "source_config_id"),
        bool_fields=("is_delete",),
        datetime_fields=("created_time",),
    ),
    "source_chunks": IndexSchema(
        name="source_chunks",
        vector_fields=("heading_vector", "content_vector"),
        text_fields=("heading", "content"),
        keyword_fields=("chunk_id", "source_id", "source_config_id", "chunk_type"),
        array_fields=("references",),
        bool_fields=("is_delete",),
        datetime_fields=("created_time", "updated_time"),
        int_fields=("rank", "content_length"),
    ),
}
