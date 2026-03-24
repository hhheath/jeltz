# Getting Started with Jeltz

This guide walks you through connecting a physical sensor to an AI assistant using Jeltz. By the end, you'll have an ESP32 reading a DHT22 temperature/humidity sensor, with an LLM querying it through MCP.

**Time:** ~20 minutes
**Hardware:** ESP32 dev board, DHT22 sensor, breadboard, jumper wires, USB cable
**Software:** Python 3.11+, Arduino IDE (or PlatformIO)

> Don't have hardware? Skip to [Testing without hardware](#testing-without-hardware) to try Jeltz with the mock adapter.

---

## 1. Install Jeltz

```bash
git clone https://github.com/hhheath/jeltz.git
cd jeltz
pip install -e .
```

Verify it works:

```bash
jeltz --version
```

## 2. Wire the hardware

Connect the DHT22 to your ESP32:

```
DHT22 Pin    ESP32 Pin
─────────    ─────────
VCC (1)  →   3.3V
DATA (2) →   GPIO4
NC (3)       (not connected)
GND (4)  →   GND
```

Add a 10K resistor between DATA and VCC (pull-up). Many DHT22 breakout boards have this built in — if yours has three pins instead of four, the pull-up is already there.

## 3. Flash the firmware

The ESP32 needs firmware that accepts text commands over serial and responds with sensor readings. Jeltz doesn't care what language the firmware is in — it just sends strings and reads strings.

### Arduino IDE

1. Install the [Arduino IDE](https://www.arduino.cc/en/software)
2. Add ESP32 board support: File → Preferences → Additional Board Manager URLs → add `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. Install "esp32" from the Board Manager
4. Install the "DHT sensor library" by Adafruit from the Library Manager

Create a new sketch and paste:

```cpp
#include <DHT.h>

#define DHT_PIN 4
#define DHT_TYPE DHT22

DHT dht(DHT_PIN, DHT_TYPE);

void setup() {
  Serial.begin(115200);
  dht.begin();
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "READ_TEMP") {
      Serial.println(dht.readTemperature());
    } else if (cmd == "READ_HUMID") {
      Serial.println(dht.readHumidity());
    } else if (cmd == "READ_ALL") {
      Serial.print(dht.readTemperature());
      Serial.print(",");
      Serial.println(dht.readHumidity());
    } else if (cmd == "STATUS") {
      float t = dht.readTemperature();
      Serial.println(isnan(t) ? "ERROR" : "OK");
    } else if (cmd == "PING") {
      Serial.println("PONG");
    }
  }
}
```

5. Select your board (e.g., "ESP32 Dev Module") and port
6. Upload

### Verify with serial monitor

Open the Arduino serial monitor (115200 baud), type `PING`, press Enter. You should see `PONG`. Type `READ_TEMP` — you should see a temperature value like `23.40`.

**Close the serial monitor before continuing.** The serial port can only have one connection at a time.

## 4. Find your serial port

The ESP32 shows up as a serial device when plugged in via USB.

**macOS:**
```bash
ls /dev/cu.usb*
# Usually /dev/cu.usbserial-0001 or /dev/cu.usbmodem14101
```

**Linux:**
```bash
ls /dev/ttyUSB* /dev/ttyACM*
# Usually /dev/ttyUSB0 or /dev/ttyACM0
```

> **Linux permissions:** If you get "Permission denied", add your user to the `dialout` group:
> ```bash
> sudo usermod -aG dialout $USER
> ```
> Then log out and back in.

Note your port — you'll need it in the next step.

## 5. Set up the profile

The repo already has a `profiles/` directory with built-in profiles. Edit `profiles/serial_sensor.toml` and set the `port` to match your device:

```toml
[connection]
protocol = "serial"
port = "/dev/cu.usbserial-0001"  # ← your port here
baud_rate = 115200
timeout_ms = 3000
```

The rest of the profile (tools, health check) matches the firmware you just flashed. If you changed the commands in the firmware, update them in the profile too.

## 6. Test the connection

```bash
jeltz test profiles/serial_sensor.toml
```

You should see:

```
Device:   serial_sensor
Protocol: serial
Tools:    4
✓ Connected
✓ Health check passed
✓ Disconnected
```

If the health check fails, check:
- Is the serial monitor closed?
- Is the port correct? (`ls /dev/cu.usb*`)
- Is the baud rate 115200 in both the firmware and the profile?
- Is the ESP32 powered and the USB cable connected?

## 7. Check device status

```bash
jeltz status -p profiles
```

```
Devices (1):

  ✓ serial_sensor [serial] — healthy
    tools: serial_sensor.get_temperature, serial_sensor.get_humidity, serial_sensor.get_reading, serial_sensor.get_status
```

## 8. Start the gateway

```bash
jeltz start -p profiles
```

```
✓ Discovered 1 device(s), exposing 8 tools
  ✓ serial_sensor
✓ MCP server ready on stdio
```

> **Note:** If you haven't removed or edited the other built-in profiles (`mqtt_sensor.toml`, `mock_sensor.toml`), the gateway will try to load them too. The mock profile will connect fine; the MQTT profile will show a connection error if you don't have a broker running. To load only your serial device, pass the profile directly: `jeltz start -p profiles/serial_sensor.toml`

The gateway is now running. It serves MCP tools over stdio — which means an MCP client can connect to it by running the `jeltz start` command as a subprocess.

Press `Ctrl+C` to stop (you'll restart it via the MCP client config in the next step).

### Daemon mode (optional)

For always-on monitoring with persistent history, use `jeltz daemon` instead:

```bash
jeltz daemon -p profiles --host 127.0.0.1 --port 8374
```

```
✓ Discovered 1 device(s), exposing 8 tools
  ✓ serial_sensor
✓ Background recording active
✓ MCP server ready on http://127.0.0.1:8374/mcp
```

Daemon mode continuously polls sensors, records readings to SQLite, and serves MCP over Streamable HTTP. Clients can connect and disconnect without affecting the recording loop — so `fleet.get_history` and `fleet.search_anomalies` have data even when no client is connected.

## 9. Connect an MCP client

### Stdio mode (client manages the process)

#### Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "jeltz": {
      "command": "jeltz",
      "args": ["start", "-p", "/absolute/path/to/your/profiles"]
    }
  }
}
```

Restart Claude Desktop. You should see Jeltz's tools appear in the tool list.

#### Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "jeltz": {
      "command": "jeltz",
      "args": ["start", "-p", "/absolute/path/to/your/profiles"]
    }
  }
}
```

Or add it via the CLI:

```bash
claude mcp add jeltz -- jeltz start -p /absolute/path/to/your/profiles
```

### Daemon mode (connect to a running gateway)

If you started Jeltz with `jeltz daemon`, point your MCP client at the HTTP endpoint:

```json
{
  "mcpServers": {
    "jeltz": {
      "url": "http://localhost:8374/mcp"
    }
  }
}
```

## 10. Talk to your sensor

Once connected, try these prompts:

> **"What's the temperature right now?"**
>
> The LLM calls `serial_sensor.get_temperature` and tells you the reading.

> **"What's the temperature and humidity?"**
>
> The LLM calls both tools (or `serial_sensor.get_reading`) and reports both values.

> **"Is the sensor working correctly?"**
>
> The LLM calls `serial_sensor.get_status` and interprets the result.

> **"Check if anything looks unusual."**
>
> The LLM calls `fleet.get_all_readings` and `fleet.search_anomalies` to analyze stored readings against baselines.

Every time a numeric tool is called, the reading is automatically stored in Jeltz's SQLite database. Over time, the LLM can use `fleet.get_history` to analyze trends and `fleet.search_anomalies` to detect drift.

---

## Testing without hardware

You can try Jeltz without any physical devices using the built-in mock profile. It simulates a temperature + humidity sensor with realistic canned responses.

First, test the mock profile:

```bash
jeltz test profiles/mock_sensor.toml
```

To start the gateway with only the mock device (avoiding connection errors from the serial/MQTT profiles), copy it into its own directory:

```bash
mkdir -p my_profiles
cp profiles/mock_sensor.toml my_profiles/
jeltz start -p my_profiles
```


The mock adapter connects instantly, passes health checks, and returns realistic values when tools are called (22.5°C, 47.3% humidity). You can customize the responses by editing the `[connection.mock_responses]` section in the profile:

```toml
[connection.mock_responses]
READ_TEMP = "22.5"
READ_HUMID = "47.3"
READ_ALL = "22.5,47.3"
STATUS = "OK"
PING = "PONG"
```

This is useful for testing your MCP client integration before hardware arrives, or for demoing Jeltz's fleet tools without a physical setup.

---

## Chat with a local LLM

If you have [Ollama](https://ollama.com/) (or any OpenAI-compatible server) running on the same machine, you can chat directly with your sensors without configuring an MCP client:

```bash
# Pull a model that supports tool calling
ollama pull llama3.2

# Start chatting (uses mock profiles — no hardware needed)
jeltz chat -p my_profiles -m llama3.2
```

```
✓ Connected to 1 device(s), exposing 5 tools
✓ LLM: llama3.2 via http://localhost:11434/v1
✓ Ready (Ctrl+C to exit)

You: what's the temperature?
  ⚙ mock_sensor.get_temperature

The current temperature reading is 22.5°C.

You:
```

The `⚙` lines show which tools the LLM is calling. You can customize the LLM endpoint:

```bash
# llama.cpp server
jeltz chat -p my_profiles --api-url http://localhost:8080/v1

# Custom model
jeltz chat -p my_profiles -m mistral

# Custom system prompt
jeltz chat -p my_profiles --system-prompt my_prompt.txt
```

This works fully offline — no internet, no cloud, no data leaving the machine. See the [README](../README.md#jeltz-chat--local-llm-interaction) for the full list of options and supported runtimes.

---

## Adding more devices

Add more TOML profiles to the `profiles/` directory — one per device. Each device gets its own namespaced tools. With two sensors, the LLM sees tools like `sensor_1.get_temperature` and `sensor_2.get_temperature` and can compare readings across them.

See the [built-in profiles](../profiles/) for more examples, or write your own following the same pattern.

## What's next

- **Try `jeltz chat`** — if you have Ollama installed, `jeltz chat -p my_profiles -m llama3.2` lets you talk to your sensors with a local LLM. No cloud required.
- **Add a second sensor** — this is where Jeltz gets interesting. Fleet tools like `fleet.get_all_readings` and `fleet.search_anomalies` shine when there are multiple devices to correlate.
- **Try an MQTT sensor** — connect a Pico W or ESP32 over WiFi. See [`profiles/mqtt_sensor.toml`](../profiles/mqtt_sensor.toml) for the setup.
- **Write a custom profile** — any device that speaks text commands over serial (or request/reply over MQTT) can be connected to Jeltz.
