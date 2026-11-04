#!/usr/bin/env python3
"""
Dimension value parser with capabilities validation.

Parses user-friendly expressions and validates against layer's available dimension values.
"""

import re
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from .parser import Layer


class DimensionParser:
    """Parse dimension values and validate against layer capabilities."""

    # Common forecast shortcuts
    FORECAST_SHORTCUTS = {
        '0h': 'PT0S',
        '1h': 'PT1H',
        '3h': 'PT3H',
        '6h': 'PT6H',
        '12h': 'PT12H',
        '24h': 'P1D',
        '48h': 'P2D',
        '2d': 'P2D',
        '3d': 'P3D',
        '4d': 'P4D',
        '5d': 'P5D',
    }

    def parse_forecast(self, expr: str, layer: Optional[Layer] = None) -> Optional[str]:
        """
        Parse forecast time expression and validate against layer.

        Args:
            expr: User input like '6h', 'PT12H', '+24h'
            layer: Layer to validate against (optional)

        Returns:
            Valid ISO duration string or None if invalid

        Examples:
            '6h' -> 'PT6H'
            'PT12H' -> 'PT12H'
            '+24h' -> 'P1D'
            '2d' -> 'P2D'
        """
        expr = expr.strip().lower().lstrip('+')

        # Check shortcuts first
        if expr in self.FORECAST_SHORTCUTS:
            parsed = self.FORECAST_SHORTCUTS[expr]
        elif expr.upper().startswith('PT') or expr.upper().startswith('P'):
            # Already ISO format
            parsed = expr.upper()
        else:
            # Try pattern: <number><unit>
            match = re.match(r'^(\d+)([hd])$', expr)
            if match:
                num, unit = match.groups()
                parsed = f'PT{num}H' if unit == 'h' else f'P{num}D'
            else:
                return None  # Can't parse

        # Validate against layer if provided
        if layer and 'FORECAST' in layer.dimensions:
            return self._validate_dimension(parsed, layer, 'FORECAST')

        return parsed

    def parse_run_time(self, expr: str, layer: Optional[Layer] = None) -> Optional[str]:
        """
        Parse run time expression and validate against layer.

        Args:
            expr: User input like '6z', 'latest', '12z'
            layer: Layer to validate against (required for validation)

        Returns:
            Valid ISO datetime string or None if invalid

        Examples:
            '6z' -> '2025-11-24T06:00:00Z' (most recent 6Z)
            'latest' -> '2025-11-24T12:00:00Z' (most recent run)
            '12z' -> '2025-11-24T12:00:00Z'
        """
        expr = expr.strip().lower()

        # Get available runs from layer
        available_runs = self._get_dimension_values(layer, 'RUN') if layer else []

        # Shortcut: latest
        if expr == 'latest':
            if available_runs:
                return max(available_runs)
            return None

        # Pattern: 6z, 12z, 00z, 18z (cycle only)
        match = re.match(r'^(\d{1,2})z$', expr)
        if match:
            cycle = int(match.group(1))
            return self._find_recent_cycle(cycle, available_runs)

        # Already ISO format - validate it
        if 'T' in expr.upper() and 'Z' in expr.upper():
            iso_expr = expr.upper()
            if available_runs:
                if iso_expr in available_runs:
                    return iso_expr
                # Find closest
                return self._find_closest_run(iso_expr, available_runs)
            return iso_expr

        return None  # Can't parse

    def parse_elevation(self, expr: str, layer: Optional[Layer] = None) -> Optional[str]:
        """
        Parse elevation/pressure level.

        Args:
            expr: User input like '850', '500mb', '10000'
            layer: Layer to validate against (optional)

        Returns:
            Valid elevation string or None if invalid

        Examples:
            '850' -> '850'
            '500mb' -> '500'
            '10000' -> '10000'
        """
        expr = expr.strip().lower()

        # Remove common suffixes
        expr = expr.replace('mb', '').replace('hpa', '').strip()

        # Should be a number
        if not expr.replace('.', '').isdigit():
            return None

        # Validate against layer if provided
        if layer and 'ELEVATION' in layer.dimensions:
            return self._validate_dimension(expr, layer, 'ELEVATION')

        return expr

    def _validate_dimension(self, parsed: str, layer: Layer, dim_name: str) -> Optional[str]:
        """Validate parsed value against layer's available dimension values."""
        available = self._get_dimension_values(layer, dim_name)

        if not available:
            return parsed  # No validation possible

        # Exact match
        if parsed in available:
            return parsed

        # Try to find closest match
        if dim_name == 'FORECAST':
            return self._find_closest_duration(parsed, available)
        elif dim_name == 'ELEVATION':
            return self._find_closest_value(parsed, available)
        else:
            # Generic closest match
            return available[0] if available else parsed

    def _get_dimension_values(self, layer: Optional[Layer], dim_name: str) -> List[str]:
        """Extract available values for a dimension from layer."""
        if not layer or dim_name not in layer.dimensions:
            return []

        dim = layer.dimensions[dim_name]

        # Try to get values
        if hasattr(dim, 'values'):
            values = dim.values
            if isinstance(values, str):
                # Comma-separated
                if ',' in values:
                    return [v.strip() for v in values.split(',')]
                # Range format: start/end/period
                elif '/' in values:
                    # Just return default for now
                    return [dim.default] if hasattr(dim, 'default') and dim.default else []
            elif isinstance(values, list):
                return values

        # Fallback to default
        if hasattr(dim, 'default') and dim.default:
            return [dim.default]

        return []

    def _find_recent_cycle(self, cycle: int, available_runs: List[str]) -> Optional[str]:
        """Find most recent run matching the cycle hour."""
        if not available_runs:
            # No runs available - construct a reasonable default
            now = datetime.utcnow()
            target = now.replace(hour=cycle, minute=0, second=0, microsecond=0)
            # If target is in the future, use yesterday
            if target > now:
                target -= timedelta(days=1)
            return target.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Find runs matching this cycle
        cycle_str = f'T{cycle:02d}:00:00Z'
        matches = [r for r in available_runs if cycle_str in r]

        if matches:
            return max(matches)  # Most recent match

        # No exact match - find closest cycle
        return self._find_closest_cycle(cycle, available_runs)

    def _find_closest_cycle(self, target_cycle: int, available_runs: List[str]) -> Optional[str]:
        """Find the available run with closest cycle to target."""
        if not available_runs:
            return None

        def extract_hour(run: str) -> int:
            """Extract hour from ISO datetime string."""
            try:
                # Format: 2025-11-23T06:00:00Z
                time_part = run.split('T')[1]
                hour = int(time_part.split(':')[0])
                return hour
            except (IndexError, ValueError):
                return 0

        # Find run with closest hour
        closest = min(available_runs,
                     key=lambda r: min(abs(extract_hour(r) - target_cycle),
                                      abs(extract_hour(r) - target_cycle + 24),
                                      abs(extract_hour(r) - target_cycle - 24)))
        return closest

    def _find_closest_run(self, target: str, available_runs: List[str]) -> Optional[str]:
        """Find closest available run to target datetime."""
        if not available_runs:
            return None

        try:
            target_dt = datetime.fromisoformat(target.replace('Z', '+00:00'))

            def parse_run(run: str) -> datetime:
                return datetime.fromisoformat(run.replace('Z', '+00:00'))

            closest = min(available_runs,
                         key=lambda r: abs((parse_run(r) - target_dt).total_seconds()))
            return closest
        except (ValueError, TypeError):
            # Can't parse - just return most recent
            return max(available_runs)

    def _find_closest_duration(self, target: str, available: List[str]) -> Optional[str]:
        """Find closest forecast duration from available values."""
        if not available:
            return None

        if target in available:
            return target

        # Convert to comparable format (hours)
        def to_hours(duration: str) -> float:
            """Convert ISO duration to hours."""
            try:
                duration = duration.upper()
                if duration == 'PT0S':
                    return 0
                if duration.startswith('PT'):
                    # PT6H format
                    if 'H' in duration:
                        return float(duration.replace('PT', '').replace('H', ''))
                    elif 'M' in duration:
                        return float(duration.replace('PT', '').replace('M', '')) / 60
                elif duration.startswith('P'):
                    # P2D format
                    if 'D' in duration:
                        return float(duration.replace('P', '').replace('D', '')) * 24
                return 0
            except ValueError:
                return 0

        target_hours = to_hours(target)
        closest = min(available, key=lambda d: abs(to_hours(d) - target_hours))
        return closest

    def _find_closest_value(self, target: str, available: List[str]) -> Optional[str]:
        """Find closest numeric value from available values."""
        if not available:
            return None

        try:
            target_val = float(target)
            closest = min(available, key=lambda v: abs(float(v) - target_val))
            return closest
        except ValueError:
            return available[0] if available else None

    def get_latest_run(self, layer: Layer) -> Optional[str]:
        """Get the latest available run time for a layer."""
        available = self._get_dimension_values(layer, 'RUN')
        return max(available) if available else None

    def get_latest_time(self, layer: Layer) -> Optional[str]:
        """Get the latest available time for a layer."""
        available = self._get_dimension_values(layer, 'TIME')
        return max(available) if available else None

    def get_available_shortcuts(self, layer: Layer, dim_name: str) -> Dict[str, str]:
        """
        Get shortcuts that are valid for this layer's dimension.

        Useful for autocomplete/suggestions.

        Returns:
            Dict of {shortcut: actual_value} for valid shortcuts only
        """
        if dim_name == 'FORECAST':
            available = self._get_dimension_values(layer, 'FORECAST')
            if not available:
                return {}

            valid_shortcuts = {}
            for shortcut, iso_value in self.FORECAST_SHORTCUTS.items():
                validated = self._validate_dimension(iso_value, layer, 'FORECAST')
                if validated:
                    valid_shortcuts[shortcut] = validated
            return valid_shortcuts

        elif dim_name == 'RUN':
            available_runs = self._get_dimension_values(layer, 'RUN')
            if not available_runs:
                return {}

            # Extract unique cycles from available runs
            cycles = set()
            for run in available_runs:
                try:
                    hour = int(run.split('T')[1].split(':')[0])
                    cycles.add(hour)
                except (IndexError, ValueError):
                    continue

            # Return shortcuts for available cycles
            shortcuts = {}
            for c in sorted(cycles):
                run = self._find_recent_cycle(c, available_runs)
                if run:
                    shortcuts[f'{c}z'] = run

            # Add 'latest'
            shortcuts['latest'] = max(available_runs)

            return shortcuts

        return {}
