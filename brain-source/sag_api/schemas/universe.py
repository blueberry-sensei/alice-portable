from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

UniverseNodeKind = Literal["event", "entity"]
UniverseNodeState = Literal["latent", "active"]
UniverseTimelineDirection = Literal["older", "newer"]


class UniverseTimeBucketOut(BaseModel):
    start: datetime
    end: datetime
    count: int = 0


class UniversePartitionOut(BaseModel):
    id: str
    source_id: str
    parent_id: str | None = None
    kind: Literal["source", "topic"]
    key: str
    label: str
    x: float
    y: float
    z: float = 0.0
    radius: float
    node_count: int
    event_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    density: float = 0.0
    time_buckets: list[UniverseTimeBucketOut] = Field(default_factory=list)
    importance: float


class UniversePolicyOut(BaseModel):
    source_limit: int
    timeline_event_page_size: int
    event_entity_limit: int
    lod_orbit_px: int
    lod_near_px: int
    lod_deep_px: int
    lod_hysteresis_px: int
    lod_debounce_ms: int
    proxy_budget_desktop: int
    proxy_budget_mobile: int
    node_budget_desktop: int
    node_budget_mobile: int
    edge_budget_desktop: int
    edge_budget_mobile: int


class UniverseManifestOut(BaseModel):
    version: str | None = None
    status: Literal["empty", "building", "ready", "stale", "failed"]
    stale: bool = False
    as_of: datetime | None = None
    bounds: dict[str, float] = Field(default_factory=dict)
    partitions: list[UniversePartitionOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    policy: UniversePolicyOut


class UniverseRelationOut(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    from_id: str = Field(min_length=1, max_length=128)
    to_id: str = Field(min_length=1, max_length=128)
    kind: Literal["mentions", "subevent"] = "mentions"
    weight: float = 1.0
    description: str = ""


class UniverseEvidenceOut(BaseModel):
    source_id: str
    source_name: str
    document_id: str | None = None
    document_name: str | None = None
    chunk_id: str | None = None
    heading: str = ""
    content: str = ""


class UniverseNodeDetailOut(BaseModel):
    id: str
    kind: UniverseNodeKind
    source_id: str
    source_name: str
    label: str
    description: str = ""
    category: str = ""
    start_time: datetime | None = None
    evidence: UniverseEvidenceOut | None = None


class UniverseExpandIn(BaseModel):
    epoch: int = Field(ge=1)
    source_id: str = Field(min_length=1, max_length=64)
    node_kind: UniverseNodeKind
    node_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=4, ge=1, le=8)
    cursor: str | None = Field(default=None, max_length=2048)
    snapshot_id: str | None = Field(default=None, max_length=2048)
    after: datetime | None = None
    before: datetime | None = None

    @model_validator(mode="after")
    def validate_time_window(self) -> UniverseExpandIn:
        if self.node_kind == "entity" and self.limit > 4:
            raise ValueError("Entity exploration returns at most four event bundles per page")
        if self.cursor is not None and self.snapshot_id is None:
            raise ValueError("A neighbourhood continuation page must carry snapshot_id")
        if self.node_kind == "event" and (self.after is not None or self.before is not None):
            raise ValueError("Expanding from an event to entities does not accept a time range")
        if self.after is not None and self.before is not None:
            after = self.after.replace(tzinfo=UTC) if self.after.tzinfo is None else self.after
            before = self.before.replace(tzinfo=UTC) if self.before.tzinfo is None else self.before
            if after.astimezone(UTC) > before.astimezone(UTC):
                raise ValueError("after cannot be later than before")
        return self


class UniverseTimelineIn(BaseModel):
    epoch: int = Field(ge=1)
    source_id: str = Field(min_length=1, max_length=64)
    # The product UI deliberately exposes a 10–50 range, while the transport
    # still accepts smaller pages for deterministic pagination probes and
    # internal callers. The public default remains the production page size.
    limit: int = Field(default=20, ge=1, le=50)
    direction: UniverseTimelineDirection = "older"
    cursor: str | None = Field(default=None, max_length=2048)
    snapshot_id: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_snapshot(self) -> UniverseTimelineIn:
        if self.cursor is not None and self.snapshot_id is None:
            raise ValueError("A timeline continuation page must carry snapshot_id")
        if self.direction == "newer" and self.cursor is None:
            raise ValueError("Continuing into a new timeline must carry a cursor")
        return self


class UniversePatchNodeOut(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    kind: UniverseNodeKind
    source_id: str = Field(min_length=1, max_length=64)
    label: str = ""
    description: str = ""
    category: str = ""
    chunk_id: str | None = None
    start_time: datetime | None = None
    importance: float = 0.5
    related_count: int = Field(default=0, ge=0)
    state: UniverseNodeState = "active"


class UniversePageOut(BaseModel):
    returned: int = Field(default=0, ge=0)
    has_more: bool = False
    next_cursor: str | None = Field(default=None, max_length=2048)


class UniverseNeighborPageOut(BaseModel):
    total_unique: int = Field(default=0, ge=0)
    returned_unique: int = Field(default=0, ge=0)
    complete: bool = False
    next_cursor: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_counts(self) -> UniverseNeighborPageOut:
        if self.returned_unique > self.total_unique:
            raise ValueError("returned_unique cannot exceed total_unique")
        if self.complete != (self.returned_unique == self.total_unique):
            raise ValueError("complete disagrees with the neighbour count")
        if self.complete != (self.next_cursor is None):
            raise ValueError("complete disagrees with the neighbour continuation cursor")
        return self


class UniverseTimelineEventOut(UniversePatchNodeOut):
    kind: Literal["event"]


class UniverseTimelineEntityOut(UniversePatchNodeOut):
    kind: Literal["entity"]


class UniverseTimelineRelationOut(UniverseRelationOut):
    kind: Literal["mentions"] = "mentions"


class UniverseTimelineBundleOut(BaseModel):
    bundle_id: str = Field(min_length=1)
    # Snapshot-stable position in the source's canonical exploration order
    # (newest = 0). The client's counting axis places the event at
    # ordinal × axis-unit, so this must never depend on which page was asked.
    ordinal: int = Field(ge=0)
    event: UniverseTimelineEventOut
    nodes: list[UniverseTimelineEntityOut] = Field(default_factory=list)
    relations: list[UniverseTimelineRelationOut] = Field(default_factory=list)
    neighbor_page: UniverseNeighborPageOut
    cursor_before: str | None = Field(default=None, max_length=2048)
    cursor_after: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_neighborhood(self) -> UniverseTimelineBundleOut:
        entity_ids = [node.id for node in self.nodes]
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("The timeline event bundle contains duplicate entities")
        entity_id_set = set(entity_ids)
        relation_keys = {(relation.from_id, relation.to_id) for relation in self.relations}
        if len(relation_keys) != len(self.relations):
            raise ValueError("The timeline event bundle contains duplicate relations")
        if any(
            relation.source_id != self.event.source_id
            or relation.from_id != self.event.id
            or relation.to_id not in entity_id_set
            for relation in self.relations
        ):
            raise ValueError("A timeline relation endpoint does not belong to the current event bundle")
        if {relation.to_id for relation in self.relations} != entity_id_set:
            raise ValueError("Every entity the timeline returns must have exactly one factual relation")
        if any(node.source_id != self.event.source_id for node in self.nodes):
            raise ValueError("The timeline event bundle spans more than one source")
        if self.neighbor_page.returned_unique != len(entity_id_set):
            raise ValueError("returned_unique disagrees with the number of entities returned")
        if self.event.related_count != self.neighbor_page.total_unique:
            raise ValueError("The total event relation count disagrees with neighbor_page")
        return self


class UniverseTimelinePageOut(BaseModel):
    returned_bundles: int = Field(default=0, ge=0)
    returned_unique_nodes: int = Field(default=0, ge=0)
    returned_relations: int = Field(default=0, ge=0)
    direction: UniverseTimelineDirection
    has_newer: bool
    newer_cursor: str | None = Field(default=None, max_length=2048)
    has_older: bool
    older_cursor: str | None = Field(default=None, max_length=2048)
    has_more: bool = False
    next_cursor: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_directional_cursors(self) -> UniverseTimelinePageOut:
        if self.has_newer != (self.newer_cursor is not None):
            raise ValueError("has_newer disagrees with newer_cursor")
        if self.has_older != (self.older_cursor is not None):
            raise ValueError("has_older disagrees with older_cursor")
        directional_cursor = self.older_cursor if self.direction == "older" else self.newer_cursor
        if self.has_more != (directional_cursor is not None):
            raise ValueError("has_more disagrees with the cursor of the requested direction")
        if self.next_cursor != directional_cursor:
            raise ValueError("next_cursor disagrees with the cursor of the requested direction")
        return self


class UniverseTimelineSliceOut(BaseModel):
    schema_version: Literal[3] = 3
    epoch: int
    source_id: str = Field(min_length=1, max_length=64)
    source_revision: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=2048)
    request_direction: UniverseTimelineDirection
    request_cursor: str | None = Field(default=None, max_length=2048)
    page_id: str = Field(min_length=1, max_length=128)
    bundles: list[UniverseTimelineBundleOut] = Field(default_factory=list)
    # Snapshot-stable event total of this source: the counting axis' length.
    total_events: int = Field(ge=0)
    page: UniverseTimelinePageOut
    as_of: datetime

    @model_validator(mode="after")
    def validate_page_contract(self) -> UniverseTimelineSliceOut:
        bundle_ids = [bundle.bundle_id for bundle in self.bundles]
        event_ids = [bundle.event.id for bundle in self.bundles]
        after_cursors = [bundle.cursor_after for bundle in self.bundles if bundle.cursor_after is not None]
        before_cursors = [bundle.cursor_before for bundle in self.bundles if bundle.cursor_before is not None]
        if len(set(bundle_ids)) != len(bundle_ids):
            raise ValueError("The timeline page contains duplicate event bundles")
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("The timeline page contains duplicate events")
        # Hydration may drop an event inside the page, so ordinals may skip;
        # they must still march strictly older within one page.
        ordinals = [bundle.ordinal for bundle in self.bundles]
        if any(later <= earlier for earlier, later in zip(ordinals, ordinals[1:], strict=False)):
            raise ValueError("Timeline event bundle ordinals must strictly increase")
        if any(ordinal >= self.total_events for ordinal in ordinals):
            raise ValueError("A timeline event bundle ordinal exceeds the source total")
        if len(set(after_cursors)) != len(after_cursors) or len(set(before_cursors)) != len(before_cursors):
            raise ValueError("The timeline page contains duplicate cursors")
        if any(bundle.event.source_id != self.source_id for bundle in self.bundles):
            raise ValueError("The timeline page spans more than one source")
        unique_nodes = {(bundle.event.kind, bundle.event.id) for bundle in self.bundles}
        unique_nodes.update((node.kind, node.id) for bundle in self.bundles for node in bundle.nodes)
        relation_count = sum(len(bundle.relations) for bundle in self.bundles)
        if self.page.returned_bundles != len(self.bundles):
            raise ValueError("returned_bundles disagrees with the number of event bundles")
        if self.page.returned_unique_nodes != len(unique_nodes):
            raise ValueError("returned_unique_nodes disagrees with the number of nodes")
        if self.page.returned_relations != relation_count:
            raise ValueError("returned_relations disagrees with the number of relations")
        if self.page.direction != self.request_direction:
            raise ValueError("The page direction disagrees with the requested direction")
        if self.page.has_more and not self.bundles:
            raise ValueError("An empty page cannot claim has_more")
        if any(bundle.cursor_after is None for bundle in self.bundles[:-1]):
            raise ValueError("A non-trailing event bundle is missing cursor_after")
        if any(bundle.cursor_before is None for bundle in self.bundles[1:]):
            raise ValueError("A non-leading event bundle is missing cursor_before")
        if self.bundles:
            if self.bundles[0].cursor_before != self.page.newer_cursor:
                raise ValueError("The leading event bundle cursor disagrees with newer_cursor")
            if self.bundles[-1].cursor_after != self.page.older_cursor:
                raise ValueError("The trailing event bundle cursor disagrees with older_cursor")
        if self.request_cursor is not None and self.request_cursor == self.page.next_cursor:
            raise ValueError("The timeline cursor did not advance")
        return self


class UniverseGraphPatchOut(BaseModel):
    schema_version: Literal[2] = 2
    epoch: int
    source_id: str = Field(min_length=1, max_length=64)
    source_revision: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=2048)
    request_cursor: str | None = Field(default=None, max_length=2048)
    page_id: str = Field(min_length=1, max_length=128)
    bundle_id: str = Field(min_length=1, max_length=512)
    anchor: UniversePatchNodeOut
    nodes: list[UniversePatchNodeOut] = Field(default_factory=list)
    relations: list[UniverseTimelineRelationOut] = Field(default_factory=list)
    page: UniversePageOut
    as_of: datetime

    @model_validator(mode="after")
    def validate_page_contract(self) -> UniverseGraphPatchOut:
        if self.anchor.source_id != self.source_id:
            raise ValueError("The exploration anchor does not belong to the current source")
        node_ids = [node.id for node in self.nodes]
        if self.anchor.id in node_ids or len(set(node_ids)) != len(node_ids):
            raise ValueError("The exploration page contains duplicate nodes")
        if any(node.source_id != self.source_id for node in self.nodes):
            raise ValueError("The exploration page spans more than one source")

        kinds_by_id = {self.anchor.id: self.anchor.kind}
        kinds_by_id.update((node.id, node.kind) for node in self.nodes)
        relation_keys = [
            (relation.from_id, relation.to_id)
            for relation in self.relations
        ]
        if len(set(relation_keys)) != len(relation_keys):
            raise ValueError("The exploration page contains duplicate relations")
        if any(
            relation.source_id != self.source_id
            or kinds_by_id.get(relation.from_id) != "event"
            or kinds_by_id.get(relation.to_id) != "entity"
            for relation in self.relations
        ):
            raise ValueError("An exploration relation endpoint is incomplete or points the wrong way")

        connected_ids = {
            endpoint
            for relation in self.relations
            for endpoint in (relation.from_id, relation.to_id)
        }
        if any(node.id not in connected_ids for node in self.nodes):
            raise ValueError("The exploration page contains nodes with no factual relation")
        if self.anchor.kind == "event":
            if any(node.kind != "entity" for node in self.nodes):
                raise ValueError("Event exploration can only return entity neighbours")
            if any(relation.from_id != self.anchor.id for relation in self.relations):
                raise ValueError("An event exploration relation must come from the anchor")
            returned = len(self.nodes)
        else:
            event_ids = {node.id for node in self.nodes if node.kind == "event"}
            if any(
                not any(
                    relation.from_id == event_id
                    and relation.to_id == self.anchor.id
                    for relation in self.relations
                )
                for event_id in event_ids
            ):
                raise ValueError("An event returned by entity exploration must connect directly to the anchor")
            returned = len(event_ids)
        if self.page.returned != returned:
            raise ValueError("returned disagrees with the primary neighbour count")
        if self.page.returned > self.anchor.related_count:
            raise ValueError("returned cannot exceed the anchor's total relation count")
        if self.page.has_more != (self.page.next_cursor is not None):
            raise ValueError("has_more disagrees with next_cursor")
        if self.page.has_more and self.page.returned == 0:
            raise ValueError("An empty exploration page cannot claim has_more")
        if (
            self.request_cursor is not None
            and self.request_cursor == self.page.next_cursor
        ):
            raise ValueError("The exploration cursor did not advance")
        return self


class ExplorationSessionOut(BaseModel):
    id: str
    title: str
    source_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    step_count: int = 0


class ExplorationStepOut(BaseModel):
    id: str
    session_id: str
    query: str
    summary: str
    source_ids: list[str] = Field(default_factory=list)
    event_refs: list[dict] = Field(default_factory=list)
    entity_refs: list[dict] = Field(default_factory=list)
    relation_refs: list[dict] = Field(default_factory=list)
    evidence_refs: list[dict] = Field(default_factory=list)
    camera: dict = Field(default_factory=dict)
    created_at: datetime


class ExplorationDetailOut(BaseModel):
    session: ExplorationSessionOut
    steps: list[ExplorationStepOut] = Field(default_factory=list)
