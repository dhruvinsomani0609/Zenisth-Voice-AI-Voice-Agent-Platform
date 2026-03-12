# 🎙️ Zenisth Voice Agent Platform

A high-performance, real-time AI Voice Agent capable of handling natural conversations, complex tool execution, and context-aware knowledge retrieval. Built with a unified **FastAPI** backend and a low-latency **WebSocket** architecture.

![Zenisth Banner](https://github.com/user-attachments/assets/46201e72-675b-43bc-bd3c-3c4d5e6f7a8b)

## 🚀 Key Features

-   **Real-Time Voice-to-Voice**: Ultra-low latency communication using Deepgram's Multimodal Live API.
-   **Dynamic Personas**: Easily configurable AI personalities and system prompts for any industry (Sales, Support, Real Estate, etc.).
-   **Function Calling**: Execute real-world tasks like booking calendar appointments via Cal.com or marking contacts for DNC.
-   **Vectorless RAG**: Instant company knowledge lookup using a hierarchical tree-based RAG pipeline (no vector database required).
-   **Unified Architecture**: Single-port deployment (HTTP + WebSocket) designed for modern cloud hosting.
-   **Aesthetic Dashboard**: Real-time transcriptions, agent state visualization, and detailed session analytics.

## 🛠️ Technology Stack

-   **Backend**: Python, FastAPI, Uvicorn, WebSockets.
-   **AI Services**: Deepgram (STT/TTS/LLM Orchestration), OpenAI (GPT-4o/FastAPI), Groq.
-   **RAG Engine**: Docling (Parsing), Redis (Caching), Supabase (Storage).
-   **Frontend**: Vanilla JS (ES6+), Modern CSS (Glassmorphism), Wavesurfer-style visualization.

## 📦 Quick Start

### 1. Prerequisites
- Python 3.10+
- Deepgram API Key

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/dhruvinsomani0609/Zenisth-Voice-AI-Voice-Agent-Platform.git
cd Zenisth-Voice-AI-Voice-Agent-Platform

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration
Create a `.env` file in the root directory:
```env
DEEPGRAM_API_KEY=your_key_here
SUPABASE_URL=your_url
SUPABASE_KEY=your_key
REDIS_URL=redis://localhost:6379
CAL_API_KEY=your_cal_key
```

### 4. Run Locally
```bash
python run.py
```
Visit `http://localhost:8080` in your browser.

## 🌐 Deployment (Cloud Ready)

This project is Dockerized and optimized for platforms like **Render**, **Railway**, or **Google Cloud Run**.

### Docker Method
```bash
docker build -t zenisth-voice-agent .
docker run -p 8080:8080 --env-file .env zenisth-voice-agent
```

## 📖 How to Customize
1.  **Change Personality**: Edit `system_prompt.txt` to define your agent's name, role, and industry.
2.  **Add Knowledge**: Upload PDFs or documentation through the **Knowledge Library** in the web dashboard to give the agent company-specific context.
3.  **Adjust Voice**: Use the **Settings Modal** to switch between different Deepgram Aura voices and adjust speed/emotion.

## ⭐ Support
If you find this project useful, please leave a star 🌟.

---
© 2026 [Zenisth AI](https://github.com/dhruvinsomani0609)
