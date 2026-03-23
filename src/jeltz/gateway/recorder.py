"""Background sensor recorder — polls devices and writes to the time-series store.

Runs as an asyncio task alongside the MCP server. Each device gets its own
polling loop at the interval specified in its profile's [recording] section.
Only tools that return numeric types are polled.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jeltz.gateway.aggregator import Aggregator, ToolRoute
    from jeltz.storage.store import ReadingStore

logger = logging.getLogger(__name__)

# Types that can be stored as float in the time-series store
_NUMERIC_TYPES = {"float", "number", "int", "integer"}


def _recordable_routes(aggregator: Aggregator) -> dict[str, list[ToolRoute]]:
    """Group recordable tool routes by device name.

    A tool is recordable if it has a command (not handler-only) and returns
    a numeric type.
    """
    by_device: dict[str, list[ToolRoute]] = {}
    for namespaced_name in aggregator.route_names:
        route = aggregator.get_route(namespaced_name)
        if route is None:
            continue
        # Skip devices with recording disabled
        if not route.device.model.recording.enabled:
            continue
        if route.command is None:
            continue
        if not route.returns or route.returns.type not in _NUMERIC_TYPES:
            continue
        # Skip tools that require parameters — we can't auto-poll them
        if route.params and any(p.required for p in route.params.values()):
            continue
        by_device.setdefault(route.device.name, []).append(route)
    return by_device


async def _poll_device(
    device_name: str,
    routes: list[ToolRoute],
    aggregator: Aggregator,
    store: ReadingStore,
    interval_s: float,
    stop_event: asyncio.Event,
) -> None:
    """Poll a single device's recordable tools at a fixed interval."""
    logger.info(
        "recorder: polling %s every %.1fs (%d sensor(s))",
        device_name, interval_s, len(routes),
    )

    while not stop_event.is_set():
        for route in routes:
            namespaced = f"{device_name}.{route.tool_name}"
            try:
                result = await aggregator.call_tool(namespaced, {})
                if not result.success:
                    logger.debug("recorder: %s returned error: %s", namespaced, result.error)
                    continue

                try:
                    value = float(result.data)
                except (TypeError, ValueError):
                    logger.debug("recorder: %s returned non-numeric: %r", namespaced, result.data)
                    continue

                unit = route.returns.unit if route.returns else ""
                await store.record(
                    device_id=device_name,
                    sensor_id=route.tool_name,
                    value=value,
                    unit=unit or "",
                )
            except Exception:
                logger.warning("recorder: failed to poll %s", namespaced, exc_info=True)

        # Wait for the interval, but break early if stop is signaled
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed, poll again


async def run_recorder(
    aggregator: Aggregator,
    store: ReadingStore,
    stop_event: asyncio.Event,
) -> None:
    """Start per-device polling loops for all recordable devices.

    Blocks until stop_event is set. Each device runs in its own task so
    a slow/stuck serial device doesn't block polling of other devices.
    """
    by_device = _recordable_routes(aggregator)

    if not by_device:
        logger.info("recorder: no recordable sensors found, background recording disabled")
        await stop_event.wait()
        return

    total_sensors = sum(len(routes) for routes in by_device.values())
    logger.info(
        "recorder: starting background recording for %d device(s), %d sensor(s)",
        len(by_device), total_sensors,
    )

    tasks: list[asyncio.Task[None]] = []
    for device_name, routes in by_device.items():
        device = routes[0].device
        interval_s = device.model.recording.poll_interval_ms / 1000.0

        task = asyncio.create_task(
            _poll_device(device_name, routes, aggregator, store, interval_s, stop_event),
            name=f"recorder:{device_name}",
        )
        tasks.append(task)

    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # Catches both regular exceptions and CancelledError (from
        # TaskGroup teardown on SIGINT/SIGTERM). Signal stop so any
        # still-running tasks can finish their current poll cycle.
        stop_event.set()
        raise
    finally:
        # Cancel any still-running tasks and wait for them to finish
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
