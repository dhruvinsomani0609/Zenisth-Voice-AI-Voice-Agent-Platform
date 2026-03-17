# Zenisth Voice — AI Voice Agent Platform

A real-time, speech-to-speech AI voice agent platform powered by the **Gemini Multimodal Live API**. The Python backend streams microphone audio to Gemini and plays the synthesised voice response back through the browser — all with sub-second latency.

---

## Features

- Real-time speech-to-speech via Gemini Multimodal Live
- Live waveform visualiser & transcript panel
- Function-calling / tool dispatch (e.g. calendar scheduling)
- Runtime config — change voice, model, and system prompt on the fly
- Pure browser frontend — no build step required

---

## Project Structure

```
Zenisth-Voice-AI-Voice-Agent-Platform/
├── app/
│   ├── server.py          # WebSocket server — bridges browser <-> Gemini Live
│   ├── config.py          # Loads config.yaml + .env
│   ├── bot.py             # Legacy Pipecat/WebRTC pipeline (not used by run.py)
│   ├── agent/             # Gemini config & tool-dispatch logic
│   └── services/          # External integrations (e.g. Cal.com)
├── index.html             # Frontend UI
├── script.js              # Frontend logic (WebSocket, AudioWorklet)
├── audio-worklet.js       # AudioWorklet processor
├── style.css              # Styles
├── config.yaml            # Server + Gemini settings
├── run.py                 # Entry point — starts both servers
└── requirements.txt       # Python dependencies
```

---

## Local Setup (VS Code)

### Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | 3.11 |
| pip | any recent |
| Google AI Studio key | [Get one free](https://aistudio.google.com/apikey) |

---

### Step 1 — Clone or update the repository

Open a terminal in VS Code (`Ctrl`+`` ` ``) and run:

```bash
# If you have not cloned yet:
git clone https://github.com/dhruvinsomani0609/Zenisth-Voice-AI-Voice-Agent-Platform.git
cd Zenisth-Voice-AI-Voice-Agent-Platform

# If you already cloned, just pull the latest commits:
git pull origin main
```

> **Tip — open the folder in VS Code:**
> ```
> code .
> ```
> Or use **File → Open Folder** and select the repo directory.

---

### Step 2 — Create a virtual environment (recommended)

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

> VS Code will detect the `.venv` automatically. If prompted, click **"Yes"** to use it as the workspace interpreter. You can also select it manually via `Ctrl+Shift+P` → **Python: Select Interpreter** → choose `.venv`.

---

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

---

### Step 4 — Configure your API key

Create a `.env` file in the **root** of the repo (next to `run.py`):

```dotenv
# Required — your Google AI Studio key
GOOGLE_API_KEY=your_google_api_key_here

# Optional — Cal.com scheduling integration
# CAL_API_KEY=your_cal_api_key
# CAL_EVENT_TYPE_ID=your_event_type_id
# DEFAULT_TIMEZONE=Asia/Kolkata
```

> **Never commit `.env`** — it is already listed in `.gitignore`.

---

### Step 5 — (Optional) Adjust settings

Open `config.yaml` to change the Gemini model, voice, or WebSocket port:

```yaml
server:
  host: "0.0.0.0"
  port: 9050          # Combined WebSocket + HTTP server port

gemini:
  model: "gemini-2.5-flash-native-audio-preview-12-2025"
  voice_name: "Kore"  # Kore | Aoede | Puck | Charon | Zephyr | Fenrir | Leda
  system_instruction: ""  # Optional default system prompt
```

---

### Step 6 — Run the server

#### Option A — VS Code debugger (one click)

A **Run Server** launch configuration is included. Press **F5** (or go to **Run → Start Debugging**) and select **"Zenisth: Run Server"**.

The debug console will show:

```
INFO:server:Starting S2S Server on 0.0.0.0:9050
INFO:server:Open browser at: http://127.0.0.1:9050
```

#### Option B — Terminal

```bash
python run.py
```

---

### Step 7 — Open the UI

Navigate to **http://127.0.0.1:9050** in your browser and click **Start Call**.

> **GitHub Codespaces:** Open the **Ports** panel, forward port `9050`, and open the generated `https://…-9050.…app.github.dev` URL. The WebSocket automatically upgrades to `wss://` — no extra configuration needed.

---

## Updating to the Latest Commit

Whenever a new fix or feature is merged, pull and restart:

```bash
git pull origin main
pip install -r requirements.txt   # only needed if requirements changed
python run.py
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `TypeError: send_realtime_input() got an unexpected keyword argument 'media_chunks'` | Pull latest — this is fixed in the current codebase. |
| `GOOGLE_API_KEY not set` | Add `GOOGLE_API_KEY=...` to your `.env` file. |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` inside your virtual env. |
| Port `9050` already in use | Change `server.port` in `config.yaml`, or stop the existing process. |
| Browser microphone blocked | Make sure the browser has microphone permission for `localhost`. |

---

## License

See [LICENSE](LICENSE).
