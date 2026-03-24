# Architecture

This document explains the technical architecture of Jeltz, the design decisions behind it, and the tradeoffs involved.

## Project structure

```
jeltz/
├── CLAUDE.md              # Claude Code instructions (keep lean)
├── README.md              # Public-facing overview and quickstart
├── ARCHITECTURE.md        # This file
├── LICENSE                # Apache 2.0
├── pyproject.toml         # Hatch build config
├── src/jeltz/
│   ├── cli/               # CLI commands (click)
│   ├── gateway/
│   │   ├── server.py      # MCP server (mcp SDK)
│   │   ├── aggregator.py  # Merges device tools, generates fleet tools
│   │   ├── fleet.py       # Fleet-level tools (list_devices, get_all_readings, get_history, search_anomalies)
│   │   └── discovery.py   # Device discovery
│   ├── profiles/
│   │   ├── parser.py      # TOML → DeviceModel
│   │   ├── generator.py   # DeviceModel → MCP tool schemas
│   │   └── validator.py   # Profile validation
│   ├── adapters/
│   │   ├── base.py        # BaseAdapter ABC
│   │   ├── serial.py      # pyserial (direct-connect prototyping)
│   │   ├── mqtt.py        # paho-mqtt
│   │   ├── modbus.py      # Modbus RTU/TCP via pymodbus (Phase 2)
│   │   ├── opcua.py       # OPC-UA via asyncua (Phase 2)
│   │   └── mock.py        # Mock for testing
│   ├── devices/
│   │   ├── model.py       # Internal device representation
│   │   ├── health.py      # Health check loop
│   │   └── state.py       # Connection state machine
│   ├── storage/
│   │   ├── store.py       # SQLite time-series store (readings, history, baselines)
│   │   └── retention.py   # Data retention policies and cleanup
│   ├── chat/
│   │   ├── client.py      # ChatClient: bridges local LLM to MCP tools
│   │   └── render.py      # Terminal rendering for chat events
│   └── events/
│       └── bus.py         # Internal event bus
├── profiles/              # Built-in TOML profiles (sensors/, platforms/, examples/)
├── tests/                 # test_profiles/, test_adapters/, test_gateway/, fixtures/
└── docs/                  # quickstart, writing-profiles, writing-adapters
```

## Current phase

**Phase 1 — MVP (6-8 weeks):** TOML profile schema with Python handler escape hatches, serial + MQTT + mock adapters, SQLite time-series storage layer, MCP server generator, gateway aggregator with fleet-level tools, device discovery and health checks, CLI, 10-15 built-in profiles for common sensors.

**Phase 2 — Industrial protocols:** Modbus RTU/TCP adapter (via `pymodbus`), OPC-UA adapter (via `asyncua`), community profile repository, device namespacing, actuator safety controls. These are the standard protocols for communicating with PLCs, RTUs, and industrial gateways — the devices that already aggregate serial sensors on the factory floor.

**Phase 3:** BLE adapter, CAN bus adapter, `jeltz-arduino` C++ library (separate repo), USB adapter, Edge Impulse MCP server federation, event streaming.

**Phase 4:** Web dashboard, alert system, IQ9 reference deployment, additional ML platform integrations.

## System overview

Jeltz is a gateway framework. It runs on a Linux host (Raspberry Pi, laptop, Qualcomm IQ9, any server) and does two things:

1. **Talks to physical devices** over their native protocols (serial, MQTT, USB, HTTP)
2. **Exposes those devices as MCP tools** through a single unified endpoint

The gateway is the only MCP endpoint. Leaf devices (ESP32, Pico, Arduino) do not run MCP servers — they speak their native protocols, and Jeltz translates.

**Why this matters beyond what a dashboard does:** The value of putting sensors behind an LLM is not "read a value in English." Any Grafana panel does that. The value is *fleet-level reasoning* — an LLM that can access 20 devices simultaneously can correlate across them (shared root cause detection), interpret anomalies in context (stuck sensor vs real reading, bearing wear vs imbalance), run multi-step investigations in one prompt ("find drifting sensors, check if the drift correlates with ambient temp, flag the ones that don't"), and surface problems an operator wasn't looking for. The architecture should always optimize for multi-device tool access, not single-sensor reads.

## Core components

### Device profiles (TOML)

A device profile is a TOML file that describes a physical device: what it is, how to connect to it, what commands it understands, and what MCP tools to generate from it.

Profiles are the primary abstraction. They decouple "how to talk to hardware" from "how to expose it to AI." A hardware engineer writes the profile. The framework handles MCP.

**Why TOML over YAML:** The target audience is embedded developers who use platformio.ini, Cargo.toml, and pyproject.toml daily. TOML is their native config language. YAML's implicit type coercion (the Norway/boolean problem) is also a liability for a framework that handles sensor values.

**The escape hatch:** Pure TOML profiles handle ~40% of real-world hardware — sensors with simple text command protocols. For the rest (binary protocols, multi-step initialization, stateful devices, custom checksums), profiles can reference a Python handler:

```toml
[device]
name = "custom_vibration"
handler = "handlers.vibration_sensor"  # Python module path
```

```python
# handlers/vibration_sensor.py
from jeltz.adapters.base import AdapterResult

async def get_reading(adapter, params):
    """Custom handler for a vibration sensor with binary protocol."""
    # Send mode command first
    await adapter.send(bytes([0x01, 0x03, params["axis"]]))
    # Read 4-byte float response
    raw = await adapter.receive(4)
    value = struct.unpack(">f", raw)[0]
    return AdapterResult.ok({"value": value, "unit": "mm/s"})
```

The profile defines the tools and connection. The handler implements the complex protocol logic. This keeps profiles readable while supporting arbitrary complexity.

### Profile parser and tool generator

The parser reads TOML profiles, validates them against a Pydantic schema, and produces an internal `DeviceModel`. The generator takes a `DeviceModel` and produces MCP tool definitions (JSON schemas) that the gateway serves.

The generator handles: tool naming and namespacing, parameter types and validation, return type schemas, and description text that helps LLMs understand what each tool does.

### Protocol adapters

Adapters handle the actual communication with physical devices. Each adapter implements a simple interface:

```python
class BaseAdapter(ABC):
    async def connect(self) -> AdapterResult
    async def disconnect(self) -> AdapterResult
    async def send(self, data: bytes | str) -> AdapterResult
    async def receive(self, length: int = None, timeout: float = None) -> AdapterResult
    async def health_check(self) -> AdapterResult
```

**Built-in adapters:**

- `SerialAdapter` — UART/serial via pyserial-asyncio. Handles baud rate, parity, flow control, reconnection. Best suited for direct-connect prototyping and lab setups. In industrial environments, serial sensors typically connect to PLCs/RTUs — use the Modbus or OPC-UA adapter to talk to those instead.
- `MQTTAdapter` — MQTT pub/sub via paho-mqtt (async wrapper). Handles topics, QoS, retained messages. Common in IIoT greenfield deployments where devices publish to a broker.
- `MockAdapter` — Simulated device for testing. Returns configurable responses. This is a first-class adapter, not an afterthought — it enables development and CI without physical hardware.

**Phase 2 adapters (industrial protocols):**

- `ModbusAdapter` — Modbus RTU (serial) and Modbus TCP via `pymodbus`. The lingua franca of industrial automation. Talks to PLCs, RTUs, VFDs, power meters, and most industrial sensors. This is how Jeltz connects to brownfield industrial environments where devices are already wired into Modbus networks.
- `OPCUAAdapter` — OPC-UA client via `asyncua`. The modern standard for industrial interoperability. Talks to SCADA systems, DCS controllers, and OPC-UA-enabled PLCs (Siemens, Beckhoff, etc.). Supports browsing server address spaces and subscribing to node value changes.

**Plugin architecture:** To add a new protocol (BLE, CAN bus, PROFINET), implement `BaseAdapter` and register it in the adapter registry. The profile's `connection.protocol` field maps to the adapter. No changes to the gateway, parser, or anything else.

**Error handling:** Adapters never raise raw exceptions to the gateway. Every method returns an `AdapterResult` that wraps success/failure with structured error context. The gateway decides how to surface errors to the MCP client (retry, report, degrade gracefully).

### Gateway and aggregator

The gateway is the MCP server that clients connect to. It uses the official `mcp` Python SDK for protocol handling (JSON-RPC, transport negotiation, tool listing, tool calling).

The aggregator is the layer that makes multi-device work. On startup (and continuously via discovery), the aggregator:

1. Finds all configured devices (from profiles and/or self-describing devices)
2. Connects to each via the appropriate adapter
3. Introspects their tool schemas
4. Namespaces the tools to avoid collisions (`device_name.tool_name`)
5. Merges everything into a single tool catalog
6. Serves the unified catalog to any MCP client

When a client calls a tool, the aggregator routes it to the correct device adapter, executes the command, and returns the result.

**Namespacing:** A device named `fermentation_temps` with a tool `get_reading` becomes `fermentation_temps.get_reading` in the unified catalog. Clients see a flat list of all tools across all devices with unambiguous names.

**Fleet-level tools:** Beyond namespaced per-device tools, the aggregator auto-generates fleet-level tools that enable the cross-device reasoning that makes Jeltz valuable:

- `fleet.list_devices` — all connected devices with health status
- `fleet.get_all_readings` — current readings from every sensor across every device in one call
- `fleet.get_history` — time-series data for any sensor from the local SQLite store (enables drift detection, trend correlation)
- `fleet.search_anomalies` — returns sensors deviating from their rolling baseline (computed from stored history)

These fleet tools are what differentiate Jeltz from "a collection of individual sensor MCP servers." They give the LLM the full picture in a single tool call, which is critical for cross-device reasoning. An LLM that has to call 20 individual `get_reading` tools sequentially will hit context limits and lose coherence. An LLM that calls `fleet.get_all_readings` once and gets the full state of the system can reason about it immediately.

### Device discovery

Two discovery mechanisms:

1. **Profile-based:** The gateway scans the `profiles/` directory on startup and loads all TOML files. Static, explicit, reliable.

2. **Network discovery (Phase 2):** Devices running the `jeltz-arduino` library broadcast their presence on the local network. The gateway listens for announcements, introspects their capabilities, and adds them to the aggregator automatically. For v1, this is not implemented — devices are added via profiles only.

### Health monitoring

Each device has a health check loop running at the interval specified in its profile. Health checks verify that the device is reachable and responding correctly. Device state follows a simple state machine:

```
DISCONNECTED → CONNECTING → CONNECTED → UNHEALTHY → CONNECTED
                                ↓
                          DISCONNECTED (after N failures)
```

Health status is available via `jeltz status` CLI command and is exposed as metadata on MCP tool responses.

### Event bus

Internal pub/sub for system events: device connected, device disconnected, health check failed, alert threshold crossed. Used by the CLI for real-time status display and by the gateway to annotate MCP responses with device state.

The event bus is internal only in v1. In Phase 3, it will be exposed as MCP resources for streaming data and alerts.

### Storage layer

Fleet reasoning requires history. An LLM can't detect drift, compare baselines, or correlate trends without stored time-series data. The devices themselves don't store it — an ESP32 has ~520KB of SRAM, not a database.

The gateway writes every sensor reading to a local SQLite database as it arrives. Each row is: timestamp, device ID, sensor ID, value, unit. This is the backing store for `fleet.get_history` and `fleet.search_anomalies`.

**Why SQLite:** Zero-config, file-based, ships with Python's stdlib, handles millions of rows without issue. A brewery with 6 sensors polling every 5 seconds generates ~50MB/year. A factory with 50 sensors polling every second generates ~4GB/year. SQLite handles both comfortably on any gateway hardware.

**Retention:** Configurable per-device in the TOML profile. Default is 30 days of full-resolution data. Older data is downsampled to hourly averages and kept for 1 year. Operators who need longer retention can increase these values or export to external time-series databases.

**What gets stored:**

```
readings table:
  timestamp   REAL     -- Unix epoch, millisecond precision
  device_id   TEXT     -- from profile name
  sensor_id   TEXT     -- from sensor definition in profile
  value       REAL     -- sensor reading
  unit        TEXT     -- celsius, mm/s, psi, etc.
  
  INDEX ON (device_id, sensor_id, timestamp)
```

**Baseline computation:** `fleet.search_anomalies` computes a rolling 30-day mean and standard deviation per sensor and flags readings beyond 2σ. This runs on the stored data, not on live reads. The baseline window and threshold are configurable.

**What is NOT stored:** Raw binary protocol data, adapter debug logs, or MCP request/response payloads. Only the parsed sensor values. Debugging data goes to the standard logging system.

### Chat client (local LLM bridge)

`jeltz chat` is a thin bridge that connects a local LLM to Jeltz's MCP tools. It is *not* an LLM runtime — it assumes something else (Ollama, llama.cpp, LM Studio, vLLM) is already serving an OpenAI-compatible API.

```
User (terminal) → jeltz chat → Local LLM (OpenAI-compatible API)
                     ↕ (in-process)
                   JeltzServer → adapters → devices
```

**How it works:** `ChatClient` starts a `JeltzServer` in-process, converts MCP tool schemas to OpenAI function-calling format, and runs a chat loop. When the LLM returns tool calls, the client executes them via `server.handle_call_tool()` and feeds the results back to the LLM. No network hops between the chat client and the gateway — it's all in the same process.

**Tool name mapping:** MCP uses dots for namespacing (`fleet.list_devices`), but some OpenAI-compatible APIs reject dots in function names. The client maps `fleet.list_devices` ↔ `fleet__list_devices` transparently.

**System prompt:** Auto-generated from live server state — lists connected devices, their protocols, health, and available tools. Overridable with `--system-prompt <file>`.

**History management:** Conversation history is maintained in-memory for the session. On error (including Ctrl+C mid-tool-call), the history is rolled back to prevent corruption.

**Optional dependency:** The `openai` Python SDK is an optional dependency (`pip install jeltz[chat]`). All other Jeltz features work without it.

## What is NOT in v1

These are deliberate omissions, not oversights:

- **Authentication on the MCP endpoint.** The gateway binds to localhost by default. Network-exposed deployments need auth — this is a known gap, documented, and planned for v2.
- **Industrial protocol adapters (Modbus, OPC-UA).** Phase 2, and the highest priority after v1. In real industrial environments, serial sensors connect to PLCs and RTUs — not directly to a gateway. Modbus RTU/TCP and OPC-UA are how Jeltz will talk to those existing systems. Serial and MQTT cover prototyping and IIoT greenfield; Modbus and OPC-UA cover brownfield.
- **Web dashboard.** Phase 4. The CLI provides all monitoring for v1.
- **Local LLM integration.** ~~Phase 4.~~ **Done.** `jeltz chat` bridges any OpenAI-compatible local LLM (Ollama, llama.cpp, LM Studio, vLLM) to Jeltz's MCP tools in-process. The LLM management (downloading models, running inference) stays with the external server — Jeltz just connects to its API. Optional dependency: `pip install jeltz[chat]`.
- **The `jeltz-arduino` library.** Phase 3. It's a separate repo with a separate language (C++) and a separate distribution channel (Arduino Library Manager / PlatformIO). v1 is profile-driven only.
- **Actuator safety controls.** If a profile defines a tool that triggers a relay or motor, there's no confirmation step or safety interlock in v1. This is documented as a warning. Phase 2 adds configurable confirmation requirements for write/actuate tools.
- **Profile registry / package manager.** Phase 2. For v1, profiles are files in a directory. Community sharing is via Git.

## Deployment tiers

The same codebase runs at three scales:

**Tier 1 — Developer workstation.** One device connected via USB. `jeltz start --device ./my_sensor.toml` boots a single MCP server. Claude Code connects directly. No gateway aggregation needed.

**Tier 2 — Small fleet (Raspberry Pi 5).** 5-20 devices on a local network. Gateway aggregates all devices behind one MCP endpoint. Optional local LLM via llama.cpp for air-gapped operation. ~$80 hardware cost.

**Tier 3 — Industrial (Qualcomm IQ9).** 20-100+ devices. IQ9 provides 100 TOPS NPU for local LLM inference (22 tok/s on Llama2-7B), 36 GB LPDDR5, industrial temp range (-40°C to 115°C), 10+ year availability. Runs the same Jeltz codebase with the local LLM integration enabled.

## Design principles

1. **Fleet reasoning, not dashboard replacement.** Jeltz is not a fancier way to read a single sensor value — that's a solved problem. The architecture optimizes for giving an LLM simultaneous access to many devices so it can correlate, interpret, investigate, and surface insights that no dashboard can. Every design decision should ask: "does this make multi-device reasoning better?"

2. **Hardware people shouldn't need to learn MCP.** The profile abstracts the protocol away. Write what you know (baud rate, commands, pin numbers), and Jeltz handles the rest.

3. **MCP people shouldn't need to learn hardware.** The tool schemas are standard MCP. A client developer doesn't need to know whether the data comes from serial, MQTT, or carrier pigeon.

4. **Local-first, offline-capable.** No cloud dependency. No internet required. Data never leaves the local network unless the user explicitly connects a cloud MCP client.

5. **Mock everything.** Every adapter has a mock equivalent. Every profile can be tested without hardware. CI runs without physical devices.

6. **Profiles are the unit of community contribution.** Writing a profile for a sensor you own and sharing it is the lowest-friction way to contribute. The profile registry should feel like the Arduino Library Manager — search, install, go.

## Known concerns and TODOs

Items to address before or during implementation. Not blockers for starting — just things to design for early so we don't paint ourselves into a corner.

- **SQLite has a ceiling for high-frequency sensors.** The 50MB-4GB/year estimates assume low-frequency polling. Vibration sensors at 10kHz+ will blow past SQLite's comfort zone. Design the storage layer with a swappable backend in mind (e.g., a `BaseStore` interface) even if SQLite is the only implementation in v1.

- **`fleet.search_anomalies` rolling 2σ is a starting point, not a solution.** It will false-positive constantly on periodic signals (HVAC cycles, batch processes) and miss slow drift. Fine for v1 — real anomaly detection comes from Edge Impulse integration in Phase 3. Be explicit in tool descriptions that this is a simple statistical baseline, not ML-based detection.

- **`fleet.get_all_readings` needs partial-result semantics.** When 3 of 20 devices are slow or unreachable, the call shouldn't block indefinitely. Define timeout and partial-result behavior: return what's available, annotate failures per-device, let the LLM reason about the gaps. Serial devices are inherently sequential per port — this will happen in practice.

- **Mock adapter must simulate fleet behavior, not just return canned values.** The compelling demo scenarios (correlated drift, stuck sensors, intermittent connectivity) require a mock that can simulate realistic multi-device dynamics. Without this, fleet reasoning features can't be developed or demoed without hardware. Consider scenario presets (e.g., `mock_scenario = "glycol_failure"`) in mock config.

- **`handler` field is a code execution vector.** A TOML profile pointing to an arbitrary Python module gets executed by the gateway. Fine for self-authored profiles, but the moment community profiles exist (Phase 2), this needs a trust model — at minimum a confirmation prompt on first load, ideally sandboxing or a review/signing mechanism. Add a one-liner warning to profile docs in v1.

- **No streaming or subscriptions in v1.** The "proactive insight" use case from the README ("anything weird?") is really "reactive insight when asked" — the LLM must poll via tool calls. This is fine given MCP resource subscriptions are still maturing, but the README should be honest about this limitation.

## Edge Impulse integration (Phase 3)

The most powerful use case for Jeltz isn't just reading sensors — it's closing the loop between physical sensor data and edge ML models. When Jeltz federates with the Edge Impulse MCP server, an LLM gets simultaneous tool access to both the raw hardware AND the ML inference pipeline.

**How it works:** Edge Impulse is building its own MCP server that exposes tools for running inference, retrieving model metrics, listing projects, and inspecting training data. The Jeltz aggregator can federate this as just another MCP endpoint alongside device tools. No special integration code — the aggregator already merges tool catalogs from multiple sources. Edge Impulse tools show up in the same unified catalog as device tools.

**What this enables — the edge AI development loop:**

An engineer deploying a vibration-based anomaly detection model on a press can say:

*"Run inference on the last 100 vibration samples from press 3. Compare the confusion matrix against the baseline from two weeks ago. The model's misclassifying more — pull the raw accelerometer data for the worst 10 misclassifications and tell me what changed in the input distribution."*

That prompt hits three tool sources in one conversation:

1. **Jeltz storage** (`fleet.get_history`) — retrieves the last 100 raw vibration readings from the SQLite store
2. **Edge Impulse MCP** (inference tools) — runs classification on those samples, returns the confusion matrix and per-sample confidence scores
3. **Jeltz device tools** (`press3.get_reading`) — pulls live data for comparison

The LLM synthesizes across all three: "The model is misclassifying more because the vibration signature shifted from 120Hz dominant to a bimodal 120/340Hz pattern over the last 48 hours. You need 30-50 additional training samples with this bimodal pattern to retrain."

That turns a half-day investigation into a 2-minute conversation. Neither Jeltz alone nor Edge Impulse alone can do this — Jeltz has the raw sensor data and history, Edge Impulse has the model and inference pipeline. Together through MCP federation, the LLM can reason across both.

**Federation config:** Add Edge Impulse as a remote MCP endpoint in the gateway config:

```toml
[[federation]]
name = "edge_impulse"
url = "http://localhost:4840/mcp"   # Edge Impulse MCP server
prefix = "ei"                       # tools become ei.run_inference, ei.get_model_metrics, etc.
```

The aggregator discovers Edge Impulse's tools, prefixes them, and merges them into the unified catalog. From the client's perspective, `fleet.get_history`, `press3.get_reading`, and `ei.run_inference` are all tools in the same flat list.
