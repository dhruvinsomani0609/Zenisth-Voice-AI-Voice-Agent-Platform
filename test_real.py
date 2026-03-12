import asyncio
import os
import json
import websockets
from dotenv import load_dotenv
from app.agent.settings import build_settings
from app.config import cfg

load_dotenv(".env")
api_key = os.environ.get("DEEPGRAM_API_KEY")

async def test_real():
    url = "wss://agent.deepgram.com/v1/agent/converse"
    headers = {"Authorization": f"Token {api_key}"}
    settings = build_settings({})
    print(json.dumps(settings, indent=2))
    
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps(settings))
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    continue
                print(f"Received: {msg}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_real())
