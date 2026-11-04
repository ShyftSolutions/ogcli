"""
ogcli - Command-line tools for OGC services (WMS + EDR)
with fuzzy search and natural dimension syntax.

Package layout:
    src/wms/    - WMS (Web Map Service) CLI
    src/edr/    - EDR (Environmental Data Retrieval) CLI
    src/common/ - Shared utilities (cache helpers, terminal display)
"""

from .wms.parser import (
    parse_capabilities,
    WMSCapabilities,
    Layer,
    BoundingBox,
    Dimension,
    Style,
    WMSParser
)

from .wms.client import (
    WMSClient,
    create_client
)

from .wms.fuzzy import (
    FuzzySearchEngine,
    SearchResult,
    LayerIndex
)

from .wms.resolver import (
    SmartQueryResolver,
    ResolvedQuery,
    AmbiguousQueryError
)

from .wms.config import (
    load_config,
    save_config,
    init_from_file,
    init_from_server,
    WMSConfig
)

__all__ = [
    # Parser
    'parse_capabilities',
    'WMSCapabilities',
    'Layer',
    'BoundingBox',
    'Dimension',
    'Style',
    'WMSParser',
    # Client
    'WMSClient',
    'create_client',
    # Fuzzy search
    'FuzzySearchEngine',
    'SearchResult',
    'LayerIndex',
    # Resolver
    'SmartQueryResolver',
    'ResolvedQuery',
    'AmbiguousQueryError',
    # Config
    'load_config',
    'save_config',
    'init_from_file',
    'init_from_server',
    'WMSConfig',
]
