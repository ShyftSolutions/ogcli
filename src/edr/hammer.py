#!/usr/bin/env python3
"""
EDR Hammer - Automated EDR load testing.

Generates a full dimension sweep across matching collections:
collections × parameters × timesteps × z-levels × random points,
fired with a thread pool. Mirrors src/wms/hammer.py.
"""

import logging
import random
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import requests

from .client import EDRClient
from .parser import EDRCollection

logger = logging.getLogger(__name__)


@dataclass
class HammerRequest:
    """One planned EDR request"""
    collection: str
    query_type: str
    params: dict
    url: str
    label: str


@dataclass
class HammerStats:
    """Run statistics"""
    total: int = 0
    success: int = 0
    failed: int = 0
    bytes: int = 0
    latencies: List[float] = field(default_factory=list)
    status_counts: dict = field(default_factory=dict)   # {http_status: count}
    start: float = 0.0
    end: float = 0.0

    def record(self, ok: bool, nbytes: int, latency: float, status: Optional[int]):
        if ok:
            self.success += 1
        else:
            self.failed += 1
        self.bytes += nbytes
        self.latencies.append(latency)
        if status is not None:
            key = str(status)
            self.status_counts[key] = self.status_counts.get(key, 0) + 1

    def report(self) -> str:
        duration = self.end - self.start if self.end else 0.0
        lines = [
            f"Requests: {self.total} total, {self.success} ok, {self.failed} failed",
        ]
        if self.status_counts:
            breakdown = "  ".join(
                f"{code}: {n}" for code, n in sorted(self.status_counts.items())
            )
            lines.append(f"Status:   {breakdown}")
        lines.append(
            f"Duration: {duration:.1f}s  ({self.total / duration:.1f} req/s)" if duration else "Duration: -"
        )
        lines.append(f"Data:     {self.bytes / 1024:.1f} KB")
        if self.latencies:
            s = sorted(self.latencies)
            avg = sum(s) / len(s)
            p50 = s[int(len(s) * 0.50)]
            p95 = s[min(int(len(s) * 0.95), len(s) - 1)]
            lines.append(
                f"Latency:  avg {avg*1000:.0f}ms  p50 {p50*1000:.0f}ms  "
                f"p95 {p95*1000:.0f}ms  max {s[-1]*1000:.0f}ms"
            )
        return "\n".join(lines)


def _sample_evenly(values: List[str], limit: Optional[int]) -> List[str]:
    """Sample up to `limit` values evenly across the list (keeps order)."""
    if not limit or limit <= 0 or len(values) <= limit:
        return values
    step = len(values) / limit
    return [values[int(i * step)] for i in range(limit)]


class EDRHammer:
    """Full-dimension-sweep load generator for EDR servers."""

    def __init__(
        self,
        base_url: str,
        collections: List[EDRCollection],
        api_key: str = "",
        workers: int = 20,
        query_type: str = "position",
        output: str = "edr_hammer_output",
        save: bool = True,
        dry_run: bool = False,
        max_timesteps: int = 0,      # 0 = all
        max_params: int = 0,         # 0 = all
        max_levels: int = 0,         # 0 = all
        points: int = 1,             # random points per collection
        limit: int = 0,              # 0 = unlimited
        timeout: int = 30,
    ):
        self.client = EDRClient(base_url, api_key=api_key, timeout=timeout)
        self.collections = collections
        self.workers = workers
        self.query_type = query_type
        self.output_dir = Path(output)
        self.save = save
        self.dry_run = dry_run
        self.max_timesteps = max_timesteps
        self.max_params = max_params
        self.max_levels = max_levels
        self.points = max(1, points)
        self.limit = limit
        self.requests: List[HammerRequest] = []

    # ------------------------------------------------------------------
    # Request generation
    # ------------------------------------------------------------------

    def _geometry_param(self, collection: EDRCollection) -> Optional[Tuple[str, str]]:
        """Geometry (param_name, value) appropriate for the query type.

        cube -> bbox=minx,miny,maxx,maxy; everything else -> coords=WKT
        """
        if not collection.bbox or len(collection.bbox) < 4:
            if self.query_type == 'cube':
                return None
            center = collection.center()
            return ('coords', f"POINT({center[0]} {center[1]})") if center else None

        minx, miny, maxx, maxy = collection.bbox
        if self.query_type == 'cube':
            return ('bbox', f"{minx},{miny},{maxx},{maxy}")

        if self.query_type == 'area':
            return ('coords',
                    f"POLYGON(({minx} {miny},{maxx} {miny},"
                    f"{maxx} {maxy},{minx} {maxy},{minx} {miny}))")

        # Point-based: random point inside the bbox, staying a bit inside the edge
        pad_x = (maxx - minx) * 0.05
        pad_y = (maxy - miny) * 0.05
        lon = random.uniform(minx + pad_x, maxx - pad_x)
        lat = random.uniform(miny + pad_y, maxy - pad_y)
        return ('coords', f"POINT({lon:.4f} {lat:.4f})")

    def generate(self) -> List[HammerRequest]:
        """Generate the full sweep of requests."""
        self.requests = []

        for collection in self.collections:
            if collection.data_queries and not collection.supports(self.query_type):
                logger.debug(f"Skipping {collection.id}: no {self.query_type} query")
                continue

            timesteps = _sample_evenly(collection.temporal_values, self.max_timesteps) or [None]
            levels = _sample_evenly(collection.vertical_values, self.max_levels) or [None]
            params = list(collection.parameter_names.keys())
            params = _sample_evenly(params, self.max_params) if params else [None]

            geometries = []
            for _ in range(self.points):
                geom = self._geometry_param(collection)
                if geom:
                    geometries.append(geom)
            if not geometries:
                continue

            count = 0
            for geom_key, geom_value in geometries:
                for param in params:
                    for t in timesteps:
                        for z in levels:
                            qtype = self.query_type
                            url = self.client._collections_url(
                                self.client.base_url, collection.id, qtype
                            )
                            request = HammerRequest(
                                collection=collection.id,
                                query_type=qtype,
                                params={
                                    geom_key: geom_value,
                                    'datetime': t,
                                    'z': z,
                                    'parameter-name': param,
                                },
                                url=url,
                                label=f"{collection.id}"
                                      f"{':' + param if param else ''}"
                                      f"{':' + t if t else ''}"
                                      f"{':z' + z if z else ''}",
                            )
                            self.requests.append(request)
                            count += 1

            if count:
                logger.info(f"  {collection.id}: {count} requests")

        if self.limit and self.limit > 0:
            self.requests = self.requests[:self.limit]

        return self.requests

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute(self, request: HammerRequest) -> tuple:
        """Run one request.

        Returns (label, ok, latency, nbytes, error, status_code).
        """
        start = time.time()
        try:
            response = self.client._get(request.url, request.params)
            latency = time.time() - start
            body = response.content
            content_type = response.headers.get('Content-Type', '')
            if self.save:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                ext = 'bin'
                if 'json' in (content_type or '').lower():
                    ext = 'json'
                elif 'netcdf' in (content_type or '').lower():
                    ext = 'nc'
                path = self.output_dir / f"{_sanitize(request.label)}.{ext}"
                path.write_bytes(body)
            return (request.label, True, latency, len(body), None, response.status_code)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            return (request.label, False, time.time() - start, 0, str(e), status)
        except Exception as e:
            return (request.label, False, time.time() - start, 0, str(e), None)

    # ------------------------------------------------------------------
    # Live status display
    # ------------------------------------------------------------------

    @staticmethod
    def _render_status(state: dict) -> "object":
        """Build the rich Live panel showing run status (no-op if no rich)."""
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich import box

        total = state['total']
        done = state['done']
        elapsed = time.time() - state['start']
        pct = (done / total) if total else 0.0
        rps = done / elapsed if elapsed > 0 else 0.0

        # Latency percentiles from latencies seen so far
        lat = sorted(state['latencies']) or [0.0]
        avg = sum(lat) / len(lat)
        p95 = lat[min(int(len(lat) * 0.95), len(lat) - 1)]

        mb = state['bytes'] / (1024 * 1024)
        kb = state['bytes'] / 1024
        size_str = f"{mb:.1f} MB" if mb >= 1 else f"{kb:.1f} KB"

        bar_len = 26
        filled = int(bar_len * pct)
        bar = Text("█" * filled + "░" * (bar_len - filled), style="cyan")

        header_line = Text("EDR Hammer  ", style="bold")
        header_line += bar
        header_line.append(f" {done}/{total}", style="bold")
        header_line.append(f" ({pct:.0%})", style="dim")
        header_line.append(f"   ✓ {state['ok']}", style="green")
        header_line.append(f"   ✗ {state['failed']}",
                           style="red" if state['failed'] else "dim")

        stats_line = Text.assemble(
            (f"{rps:.1f} req/s", "bold cyan"),
            (f"   avg {avg*1000:.0f}ms   p95 {p95*1000:.0f}ms", ""),
            (f"   {size_str} received", ""),
        )

        header = Group(header_line, stats_line)

        recent = Table(
            box=box.SIMPLE_HEAD, pad_edge=False, expand=False,
            show_edge=False,
        )
        recent.add_column("time", style="dim", no_wrap=True)
        recent.add_column("request", no_wrap=True, max_width=48, overflow="ellipsis")
        recent.add_column("status", justify="right", no_wrap=True)
        recent.add_column("size", justify="right", no_wrap=True)
        recent.add_column("ms", justify="right", no_wrap=True)

        for row in state['recent']:
            ts, label, status, nbytes, latency = row
            if status is None:
                status_str = Text("ERR", style="red")
            elif status < 300:
                status_str = Text(str(status), style="green")
            elif status < 500:
                status_str = Text(str(status), style="yellow")
            else:
                status_str = Text(str(status), style="red")
            if nbytes >= 1024 * 1024:
                size = f"{nbytes / (1024*1024):.1f}M"
            elif nbytes > 0:
                size = f"{nbytes / 1024:.1f}K"
            else:
                size = "-"
            recent.add_row(ts, label, status_str, size, f"{latency*1000:.0f}")

        parts = [header, recent]
        if state['last_error']:
            err = state['last_error']
            if len(err) > 100:
                err = err[:100] + "..."
            parts.append(Text(f"last error: {err}", style="red"))

        return Panel(Group(*parts), border_style="cyan")

    def run(self) -> HammerStats:
        """Execute the sweep."""
        if not self.requests:
            self.generate()

        stats = HammerStats(total=len(self.requests))

        if self.dry_run:
            logger.info(f"Dry run: {len(self.requests)} requests would be sent")
            for req in self.requests[:20]:
                print(f"  {req.url}?{'&'.join(f'{k}={v}' for k, v in req.params.items() if v)}")
            if len(self.requests) > 20:
                print(f"  ... and {len(self.requests) - 20} more")
            return stats

        use_live = sys.stdout.isatty()
        if use_live:
            try:
                from rich.live import Live
            except ImportError:
                use_live = False

        state = {
            'total': stats.total, 'done': 0, 'ok': 0, 'failed': 0,
            'bytes': 0, 'latencies': [], 'recent': deque(maxlen=8),
            'last_error': None, 'start': time.time(),
        }

        def handle_result(result, live=None):
            label, ok, latency, nbytes, error, status = result
            state['done'] += 1
            state['bytes'] += nbytes
            state['latencies'].append(latency)
            if ok:
                state['ok'] += 1
            else:
                state['failed'] += 1
                state['last_error'] = f"{label}: {error}" if error else label
            stats.record(ok, nbytes, latency, status)
            state['recent'].append(
                (time.strftime('%H:%M:%S'), label, status, nbytes, latency)
            )
            if live is not None:
                live.update(self._render_status(state))

        if use_live:
            with Live(self._render_status(state), refresh_per_second=10,
                      transient=False) as live:
                stats.start = time.time()
                state['start'] = stats.start
                with ThreadPoolExecutor(max_workers=self.workers) as executor:
                    futures = [executor.submit(self._execute, r) for r in self.requests]
                    for future in as_completed(futures):
                        handle_result(future.result(), live=live)
                stats.end = time.time()
                live.update(self._render_status(state))
        else:
            # Non-TTY fallback: plain progress lines
            stats.start = time.time()
            logger.info(
                f"Hammering {stats.total} requests with {self.workers} workers..."
            )
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [executor.submit(self._execute, r) for r in self.requests]
                for future in as_completed(futures):
                    result = future.result()
                    handle_result(result)
                    label, ok, _lat, _n, error, status = result
                    if not ok:
                        err = error or ''
                        if len(err) > 90:
                            err = err[:90] + '...'
                        print(f"  ✗ [{status or 'ERR'}] {label}: {err}")
                    done = state['done']
                    if done % 25 == 0 or done == stats.total:
                        elapsed = time.time() - stats.start
                        print(
                            f"  {done}/{stats.total}  ok={state['ok']} "
                            f"fail={state['failed']}  {state['bytes']/1024:.1f}KB  "
                            f"{done/elapsed:.1f} req/s"
                        )
            stats.end = time.time()

        return stats


def _sanitize(name: str) -> str:
    """Sanitize a label for use as a filename"""
    return (
        name.replace(':', '-')
            .replace('/', '-')
            .replace(' ', '_')
            .replace('?', '')
            .replace('&', '_')
    )
