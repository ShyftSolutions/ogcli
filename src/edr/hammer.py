#!/usr/bin/env python3
"""
EDR Hammer - Automated EDR load testing.

Generates a full dimension sweep across matching collections:
collections × parameters × timesteps × z-levels × random points,
fired with a thread pool. Mirrors src/wms/hammer.py.
"""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

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
    start: float = 0.0
    end: float = 0.0

    def report(self) -> str:
        duration = self.end - self.start if self.end else 0.0
        lines = [
            f"Requests: {self.total} total, {self.success} ok, {self.failed} failed",
            f"Duration: {duration:.1f}s  ({self.total / duration:.1f} req/s)" if duration else "Duration: -",
            f"Data:     {self.bytes / 1024:.1f} KB",
        ]
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
        """Run one request. Returns (label, ok, latency, nbytes, error)."""
        start = time.time()
        try:
            body, content_type = self.client.query(
                request.collection,
                request.query_type,
                **request.params,
            )
            latency = time.time() - start
            if self.save:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                ext = 'bin'
                if 'json' in (content_type or ''):
                    ext = 'json'
                elif 'netcdf' in (content_type or '').lower():
                    ext = 'nc'
                path = self.output_dir / f"{_sanitize(request.label)}.{ext}"
                path.write_bytes(body)
            return (request.label, True, latency, len(body), None)
        except Exception as e:
            latency = time.time() - start
            return (request.label, False, latency, 0, str(e))

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

        logger.info(f"Hammering {len(self.requests)} requests with {self.workers} workers...")
        stats.start = time.time()

        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._execute, r): r for r in self.requests}
            for future in as_completed(futures):
                label, ok, latency, nbytes, error = future.result()
                done += 1
                stats.latencies.append(latency)
                stats.bytes += nbytes
                if ok:
                    stats.success += 1
                else:
                    stats.failed += 1
                    logger.error(f"  ✗ {label}: {error}")
                if done % 50 == 0:
                    logger.info(f"  ... {done}/{stats.total}")

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
