import asyncio
import os
import json
import websockets
from dotenv import load_dotenv

load_dotenv(".env")
api_key = os.environ.get("DEEPGRAM_API_KEY")

async def test_deepgram(model="aura-2-helios-en"):
    url = "wss://agent.deepgram.com/v1/agent/converse"
    headers = {"Authorization": f"Token {api_key}"}
    settings = {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "linear16", "sample_rate": 16000},
            "output": {"encoding": "linear16", "sample_rate": 16000}
        },
        "agent": {
            "listen": {"provider": {"type": "deepgram", "model": "nova-2"}},
            "think": {"provider": {"type": "open_ai", "model": "gpt-4o-mini"}},
            "speak": {"provider": {"type": "deepgram", "model": model}}
        }
    }
    
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps(settings))
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    continue
                print(f"[{model}] Received: {msg}")
    except Exception as e:
        print(f"[{model}] Exception: {e}")

async def main():
    await test_deepgram("aura-2-helios-en")
    print("-" * 40)
    await test_deepgram("aura-helios-en")
    print("-" * 40)
    await test_deepgram("aura-2-phoebe-en")

if __name__ == "__main__":
    asyncio.run(main())
