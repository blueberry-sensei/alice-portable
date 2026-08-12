"""File upload connector - the static connector built into the MVP.

The user uploads documents directly through the API without discover/fetch; this connector mainly supplies metadata and configuration validation,
and is the first concrete implementation of the "ingestion layer abstraction", setting the interface pattern for the dynamic connectors to come.
"""

from __future__ import annotations

from sag_api.connectors.base import Connector, ConnectorMeta
from sag_api.enums import ConnectorKind


class FileUploadConnector(Connector):
    meta = ConnectorMeta(
        kind=ConnectorKind.FILE_UPLOAD,
        title="File upload",
        description="Upload local documents (Markdown / text / PDF and more); the engine parses, chunks and vectorises them and extracts events and entities.",
        supports_sync=False,
        config_fields=[],
    )
