"""Shared fuzzy name completion used by the edr and wms completers."""

try:
    from rapidfuzz import fuzz
    _HAVE_FUZZ = True
except ImportError:  # pragma: no cover
    _HAVE_FUZZ = False


def fuzzy_names(names, incomplete, limit=50):
    """
    Prefix/substring matches first, then fuzzy (partial_ratio > 60).

    Args:
        names: list of (value, help) tuples
        incomplete: the partial word being completed
        limit: max candidates
    """
    from click.shell_completion import CompletionItem

    if not incomplete:
        return [CompletionItem(v, help=h) for v, h in names[:limit]]

    incomplete_lower = incomplete.lower()
    prefix, substr, fuzzy = [], [], []

    for value, help_ in names:
        v = value.lower()
        if v.startswith(incomplete_lower):
            prefix.append((value, help_))
        elif incomplete_lower in v:
            substr.append((value, help_))
        elif _HAVE_FUZZ:
            score = fuzz.partial_ratio(incomplete_lower, v)
            if score > 60:
                fuzzy.append((score, value, help_))

    fuzzy.sort(key=lambda t: t[0], reverse=True)
    out = prefix + substr
    out.extend((v, h) for _, v, h in fuzzy)
    return [CompletionItem(v, help=h) for v, h in out[:limit]]
