import asyncio
import os
import json
import websockets
from dotenv import load_dotenv

load_dotenv(".env")
api_key = os.environ.get("DEEPGRAM_API_KEY")

async def test_speak(model):
    url = "wss://agent.deepgram.com/v1/agent/converse"
    headers = {"Authorization": f"Token {api_key}"}
    settings = {
        "type": "Settings",
        "agent": {
            "listen": {"provider": {"type": "deepgram", "model": "nova-2"}},
            "think": {"provider": {"type": "open_ai", "model": "gpt-4o-mini"}},
            "speak": {"provider": {"type": "deepgram", "model": model}},
            "greeting": "Testing one two three."
        }
    }
    
    print(f"\n--- Testing {model} ---")
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps(settings))
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    print("Received Audio Bytes!")
                    break
                else:
                    data = json.loads(msg)
                    print(f"Received JSON: {data}")
                    if data.get("type") == "Error":
                        break
    except Exception as e:
        print(f"Exception: {e}")

async def main():
    await test_speak("aura-helios-en")
    await test_speak("aura-2-helios-en")

if __name__ == "__main__":
    asyncio.run(main())
