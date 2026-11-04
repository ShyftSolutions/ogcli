"""Shell completion helpers for the EDR CLI."""

from click.shell_completion import CompletionItem


def complete_collection_names(ctx, param, incomplete):
    """
    Provide EDR collection name completions based on partial input.
    Falls back gracefully if EDR is not initialized.
    """
    try:
        from .config import load_edr_config, get_collections_json
        from .parser import parse_collections

        config = load_edr_config()
        if not config.is_initialized():
            return []

        caps = parse_collections(get_collections_json(config), config.server_url)

        incomplete_lower = incomplete.lower()
        completions = []
        for collection in caps.collections:
            if (collection.id.lower().startswith(incomplete_lower)
                    or incomplete_lower in collection.id.lower()):
                completions.append(
                    CompletionItem(collection.id, help=collection.title or "")
                )

        return completions[:50]

    except Exception:
        # Fail silently - completion should never break the shell
        return []
