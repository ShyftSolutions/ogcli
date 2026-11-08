"""Shell completion helpers for the WMS CLI.

Fuzzy: prefix/substring matches first, then partial-ratio fuzzy
matches (score > 60), so 'temp' completes 'gfs_TMP'-style names.
"""

from click.shell_completion import CompletionItem


def complete_layer_names(ctx, param, incomplete):
    """
    Fuzzy-complete WMS layer names from the active profile's cache.

    Falls back gracefully if WMS is not initialized. Never hits the
    network (completions must be instant).
    """
    try:
        from .config import load_config, get_capabilities_xml
        from .parser import parse_capabilities

        config = load_config()
        if not config.is_initialized():
            return []

        caps = parse_capabilities(get_capabilities_xml(config))
        names = [
            (l.name, l.title or "")
            for l in caps.get_queryable_layers()
            if l.name
        ]
        from ..common.completions import fuzzy_names
        return fuzzy_names(names, incomplete)

    except Exception:
        return []


def complete_group_names(ctx, param, incomplete):
    """Complete layer group prefixes (GFS, GALWEM, etc.)."""
    try:
        from .config import load_config, get_capabilities_xml
        from .parser import parse_capabilities

        config = load_config()
        if not config.is_initialized():
            return []

        caps = parse_capabilities(get_capabilities_xml(config))
        layers = caps.get_queryable_layers()

        groups = set()
        for layer in layers:
            if layer.name and '_' in layer.name:
                groups.add(layer.name.split('_')[0])

        return [
            CompletionItem(g)
            for g in sorted(groups)
            if g.upper().startswith(incomplete.upper())
        ]
    except Exception:
        return []


def complete_profile_names(ctx, param, incomplete):
    """Complete saved WMS profile names."""
    try:
        from .config import load_config
        config = load_config()
        return [
            CompletionItem(p, help="active" if p == config.active else "")
            for p in sorted(config.profile_names())
            if p.startswith(incomplete)
        ]
    except Exception:
        return []
