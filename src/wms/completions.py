"""Shell completion helpers for WMS CLI."""

from click.shell_completion import CompletionItem


def complete_layer_names(ctx, param, incomplete):
    """
    Provide layer name completions based on partial input.

    Returns layer names that match the incomplete string.
    Falls back gracefully if WMS is not initialized.
    """
    try:
        from .config import load_config, get_capabilities_xml
        from .parser import parse_capabilities

        config = load_config()
        if not config.is_initialized():
            return []

        xml_content = get_capabilities_xml(config)
        caps = parse_capabilities(xml_content)
        layers = caps.get_queryable_layers()

        # Filter layers matching the incomplete string
        incomplete_lower = incomplete.lower()
        completions = []

        for layer in layers:
            if not layer.name:
                continue

            name_lower = layer.name.lower()

            # Match if incomplete is prefix or appears anywhere in name
            if name_lower.startswith(incomplete_lower) or incomplete_lower in name_lower:
                completions.append(
                    CompletionItem(layer.name, help=layer.title or "")
                )

        # Limit to reasonable number
        return completions[:50]

    except Exception:
        # Fail silently - completion should never break the shell
        return []


def complete_group_names(ctx, param, incomplete):
    """
    Provide group/model name completions (GFS, GALWEM, etc.).
    """
    try:
        from .config import load_config, get_capabilities_xml
        from .parser import parse_capabilities

        config = load_config()
        if not config.is_initialized():
            return []

        xml_content = get_capabilities_xml(config)
        caps = parse_capabilities(xml_content)
        layers = caps.get_queryable_layers()

        # Extract unique group prefixes from layer names
        groups = set()
        for layer in layers:
            if layer.name and '_' in layer.name:
                groups.add(layer.name.split('_')[0])

        incomplete_upper = incomplete.upper()
        return [
            CompletionItem(g)
            for g in sorted(groups)
            if g.upper().startswith(incomplete_upper)
        ]

    except Exception:
        return []
