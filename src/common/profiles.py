#!/usr/bin/env python3
"""Interactive profile picker shared by the wms and edr CLIs."""

import sys


def pick_profile_interactive(profiles, active, tool):
    """
    Interactively pick a profile from a numbered list.

    Args:
        profiles: list of (name, detail) tuples
        active: currently active profile name
        tool: cli name for hint messages ('edr' / 'wms')

    Returns:
        Chosen profile name (blank input returns `active`)

    Raises:
        RuntimeError: when stdin is not a TTY (so scripts fail loudly
            instead of hanging); the message lists the profiles
    """
    if not sys.stdin.isatty():
        listing = "\n".join(f"  {name:<20} {detail}" for name, detail in profiles)
        raise RuntimeError(
            f"interactive picker requires a terminal. Profiles:\n{listing}\n"
            f"Use '{tool} set profile NAME' or '{tool} --profile NAME <command>'"
        )

    print("Saved profiles:")
    for i, (name, detail) in enumerate(profiles, 1):
        marker = "*" if name == active else " "
        print(f" {marker} {i:2}. {name:<20} {detail}")
    try:
        line = input(
            f"Select profile [1-{len(profiles)}] (blank = keep '{active}'): "
        ).strip()
    except EOFError:
        return active

    if not line:
        return active
    if line.isdigit():
        n = int(line)
        if 1 <= n <= len(profiles):
            return profiles[n - 1][0]
        raise RuntimeError(
            f"invalid selection {n} (expected 1-{len(profiles)})"
        )
    for name, _ in profiles:
        if name == line:
            return name
    raise RuntimeError(f"no profile named '{line}'")
