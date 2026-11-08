"""Shell completion helpers for the EDR CLI.

Fuzzy: prefix/substring matches first, then partial-ratio fuzzy
matches (score > 60), so 'temp' completes 'gfs_TMP'-style names.
"""

from click.shell_completion import CompletionItem


def complete_collection_names(ctx, param, incomplete):
    """
    Fuzzy-complete EDR collection ids from the active profile's cache.

    Falls back gracefully if EDR is not initialized. Never hits the
    network (completions must be instant).
    """
    try:
        from .config import load_edr_config, get_collections_json
        from .parser import parse_collections

        config = load_edr_config()
        if not config.is_initialized():
            return []

        caps = parse_collections(get_collections_json(config), config.server_url)
        names = [(c.id, c.title or "") for c in caps.collections]
        return _fuzzy_names(names, incomplete)

    except Exception:
        # Fail silently - completion should never break the shell
        return []


def complete_profile_names(ctx, param, incomplete):
    """Complete saved EDR profile names."""
    try:
        from .config import load_edr_config
        config = load_edr_config()
        return [
            CompletionItem(p, help="active" if p == config.active else "")
            for p in sorted(config.profile_names())
            if p.startswith(incomplete)
        ]
    except Exception:
        return []


def _fuzzy_names(names, incomplete, limit=50):
    """Prefix/substring matches first, then fuzzy (partial_ratio > 60)."""
    if not incomplete:
        return [CompletionItem(v, help=h) for v, h in names[:limit]]

    incomplete_lower = incomplete.lower()
    prefix, substr, fuzzy = [], [], []
    try:
        from rapidfuzz import fuzz
        have_fuzz = True
    except ImportError:
        have_fuzz = False

    for value, help_ in names:
        v = value.lower()
        if v.startswith(incomplete_lower):
            prefix.append((value, help_))
        elif incomplete_lower in v:
            substr.append((value, help_))
        elif have_fuzz:
            score = fuzz.partial_ratio(incomplete_lower, v)
            if score > 60:
                fuzzy.append((score, value, help_))

    fuzzy.sort(key=lambda t: t[0], reverse=True)
    out = prefix + substr
    out.extend((v, h) for _, v, h in fuzzy)
    return [CompletionItem(v, help=h) for v, h in out[:limit]]
