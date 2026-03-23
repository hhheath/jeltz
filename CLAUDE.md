# CLAUDE.md

Jeltz is an open source MCP gateway that connects AI assistants to physical devices (sensors, actuators, edge ML boards). Python framework, runs on Linux (Pi, IQ9, laptop), translates between MCP protocol and native device protocols.

See @README.md for project overview. See @ARCHITECTURE.md for detailed design decisions and rationale.

## Tech stack

Python 3.11+, `mcp` SDK, `pyserial`, `paho-mqtt`, `tomllib`, `click`, `pydantic`, `aiosqlite`, `pytest` + `pytest-asyncio`, `hatch` build system.

## Commands

```bash
# Development
hatch run test       # Run test suite (305 tests, mock adapter, no hardware needed)
hatch run lint       # Ruff linter
hatch run typecheck  # Mypy

# CLI (all working)
jeltz start -p profiles      # Start MCP gateway (stdio transport)
jeltz daemon -p profiles     # Long-running daemon: background recording + HTTP
jeltz status -p profiles     # Show connected devices and health
jeltz test <profile.toml>    # Test a single device connection
jeltz add-device <file.toml> # Validate and copy a profile into profiles/
```

## Hard constraints

IMPORTANT: These are non-negotiable for v1.

- **MCP runs on the gateway, NEVER on leaf devices.** Microcontrollers speak native protocols (serial, MQTT). The gateway translates.
- **Fleet reasoning is the value.** Jeltz is not a dashboard replacement. Every feature should enable multi-device correlation, anomaly interpretation, and cross-device investigation — not single-sensor reads. The aggregator generates fleet-level tools (`fleet.list_devices`, `fleet.get_all_readings`, `fleet.get_history`, `fleet.search_anomalies`) alongside per-device tools.
- **SQLite for time-series storage.** The gateway stores every sensor reading locally. Without history, there is no drift detection, no baseline comparison, no anomaly search. `fleet.get_history` and `fleet.search_anomalies` query this store. Use `aiosqlite` for async access. IMPORTANT: Data retention must be configurable and enforced from day one — default 30 days full resolution, 1 year downsampled hourly. A cleanup job runs on gateway startup and periodically. Never ship storage without retention.
- **Pure Python.** No Rust, no compiled extensions, no C bindings. Use pyserial, paho-mqtt, etc.
- **TOML for device profiles, not YAML.** Profiles have Python handler escape hatches for complex protocols via a `handler` field.
- **Gateway aggregates, not proxies.** Single unified MCP endpoint. Tools are namespaced: `device_name.tool_name`.

## Do NOT

- Add Rust or compiled extensions
- Add a web dashboard (Phase 3)
- Add local LLM integration / llama.cpp (Phase 3)
- Add auth to the MCP endpoint (v2)
- Run MCP servers on microcontrollers
- Over-engineer the profile schema — start minimal

## Code patterns

- Async everywhere I/O is involved (gateway, adapters, health checks)
- Type hints on everything. Pydantic models for data crossing boundaries.
- Adapters implement `BaseAdapter` (see `src/jeltz/adapters/base.py`). Never raise raw exceptions — return `AdapterResult`.
- Mock adapter is first-class. All tests run against mock, no hardware in CI.

## Testing

Run single tests during development, not the full suite:
```bash
hatch run test tests/test_profiles/test_parser.py -k "test_name"
```
CI uses GitHub Actions with mock adapter only. Hardware tests are manual, documented in `tests/hardware/`.
