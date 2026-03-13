import asyncio
import io
import json
import os
import sys
import threading
import yaml
from datetime import datetime
from pathlib import Path

import websockets
from fastapi import FastAPI, WebSocket, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.config import cfg
from app.agent.settings import build_settings
from app.agent.functions import dispatch

# ── Setup ──
UPLOADS_DIR = cfg.base_dir / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
_UPLOADS_INDEX = UPLOADS_DIR / "index.json"

app = FastAPI(title="Zenisth Voice Agent")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _load_uploads_index() -> dict:
    if _UPLOADS_INDEX.exists():
        try:
            return json.loads(_UPLOADS_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _update_uploads_index(doc_id: str, filename: str, file_size: int) -> None:
    index = _load_uploads_index()
    index[filename] = {
        "document_id": doc_id,
        "file_size": file_size,
        "uploaded_at": datetime.now().isoformat(),
    }
    _UPLOADS_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")

def _remove_from_uploads_index(filename: str) -> None:
    index = _load_uploads_index()
    if filename in index:
        del index[filename]
        _UPLOADS_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")

_SIMPLE_EVENTS: dict[str, str] = {
    "SettingsApplied": "connected",
    "AgentThinking": "agent_thinking",
    "AgentStartedSpeaking": "agent_speaking",
    "AgentAudioDone": "agent_done",
    "UserStartedSpeaking": "user_speaking",
}

_SIDE_EFFECTS: dict[str, str] = {
    "book_appointment": "appointment_booked",
    "mark_dnc": "dnc_marked",
    "transfer_to_human": "transfer_initiated",
    "end_call": "call_ended",
}

# ── API Routes ──

@app.get("/api/models")
async def get_models():
    return cfg.models

@app.get("/api/config")
async def get_config():
    # Return defaults + system prompt if exists
    data = cfg.defaults.copy()
    prompt_path = cfg.base_dir / "system_prompt.txt"
    if prompt_path.exists():
        data["system_prompt"] = prompt_path.read_text(encoding="utf-8")
    return data

@app.get("/api/documents")
async def list_documents():
    try:
        from supabase._async.client import create_client
        index = _load_uploads_index()
        disk_files = []

        for fname, meta in index.items():
            file_path = UPLOADS_DIR / fname
            if not file_path.exists(): continue
            stat = file_path.stat()
            doc_id = meta.get("document_id", "")
            disk_files.append({
                "document_id": doc_id,
                "filename": fname,
                "file_size": stat.st_size,
                "modified_at": meta.get("uploaded_at", datetime.fromtimestamp(stat.st_mtime).isoformat()),
                "status": "uploaded",
                "node_count": 0,
                "created_at": "",
            })

        disk_files.sort(key=lambda x: x["modified_at"], reverse=True)

        # Supabase check
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        ingested_map = {}
        if url and key:
            client = await create_client(url, key)
            resp = await client.table("document_nodes").select("document_id, created_at").execute()
            for row in resp.data:
                did = row["document_id"]
                if did not in ingested_map:
                    ingested_map[did] = {"node_count": 0, "created_at": row.get("created_at", "")}
                ingested_map[did]["node_count"] += 1

        seen_doc_ids = {e["document_id"] for e in disk_files}
        for did, info in ingested_map.items():
            if did not in seen_doc_ids:
                disk_files.append({
                    "document_id": did,
                    "filename": did + " (file not on disk)",
                    "file_size": 0,
                    "modified_at": info["created_at"],
                    "status": "ready",
                    "node_count": info["node_count"],
                    "created_at": info["created_at"],
                })

        for entry in disk_files:
            did = entry["document_id"]
            if did in ingested_map:
                entry["status"] = "ready"
                entry["node_count"] = ingested_map[did]["node_count"]
                entry["created_at"] = ingested_map[did]["created_at"]

        return disk_files
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    import re
    try:
        filename = Path(file.filename).name
        ext = Path(filename).suffix.lower()
        if ext not in {".pdf", ".docx", ".txt"}:
            return JSONResponse({"error": f"Unsupported type '{ext}'"}, 400)

        doc_id = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")
        dest = UPLOADS_DIR / filename
        content = await file.read()
        dest.write_bytes(content)
        
        _update_uploads_index(doc_id, filename, len(content))
        return {"status": "uploaded", "document_id": doc_id, "filename": filename, "file_size": len(content)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@app.post("/api/activate")
async def activate_document(request: Request):
    try:
        body = await request.json()
        doc_id = body.get("document_id", "").strip()
        filename = body.get("filename", "").strip()
        if not doc_id or not filename:
            return JSONResponse({"error": "document_id and filename required"}, 400)

        dest = UPLOADS_DIR / filename
        if not dest.exists():
            return JSONResponse({"error": "File not found"}, 404)

        # Run ingestion in a background task or just await it if it's quick
        # For simplicity and responsiveness, we'll do it in a thread like before but handle it more cleanly
        from app.services.ingestion import ingest_document
        from app.services.storage import insert_nodes, cache_document_outline
        from supabase._async.client import create_client as _cc

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        
        async def _check_existing():
            if not url or not key: return 0
            client = await _cc(url, key)
            resp = await client.table("document_nodes").select("node_id", count="exact").eq("document_id", doc_id).limit(1).execute()
            return resp.count or len(resp.data)

        existing = await _check_existing()
        if existing:
            return {"status": "already_ingested", "document_id": doc_id, "node_count": existing}

        print(f"[activate] Ingesting '{dest}'...")
        nodes = await ingest_document(str(dest), doc_id)
        await insert_nodes(nodes)
        await cache_document_outline(doc_id)
        
        return {"status": "ok", "document_id": doc_id, "node_count": len(nodes)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, 500)

@app.post("/api/config/save")
async def save_config(request: Request):
    try:
        updates = await request.json()
        config_path = cfg.base_dir / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        agent = raw.setdefault("agent", {})
        defaults = agent.setdefault("defaults", {})

        allowed = {"voice_model", "stt_model", "llm_model", "temperature", "greeting", 
                   "voice_speed", "voice_emotion", "barge_in", "endpointing", "filler_audio"}
        
        for k, v in updates.items():
            if k in allowed:
                defaults[k] = v
            elif k == "system_prompt":
                (cfg.base_dir / "system_prompt.txt").write_text(v, encoding="utf-8")

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

        cfg._raw = raw
        return {"status": "saved"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@app.delete("/api/documents")
async def delete_document(request: Request):
    try:
        body = await request.json()
        doc_id = body.get("document_id", "").strip()
        filename = body.get("filename", "").strip()
        
        from supabase._async.client import create_client
        import redis.asyncio as aioredis

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        if url and key:
            client = await create_client(url, key)
            await client.table("document_nodes").delete().eq("document_id", doc_id).execute()
        
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        r = await aioredis.from_url(redis_url)
        await r.delete(f"outline:{doc_id}")
        await r.aclose()

        if filename:
            file_path = UPLOADS_DIR / filename
            if file_path.exists(): file_path.unlink()
            _remove_from_uploads_index(filename)

        return {"status": "deleted", "document_id": doc_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

# ── WebSocket relay ──

async def _relay_mic_to_dg(browser_ws: WebSocket, dg_ws):
    try:
        async for msg in browser_ws.iter_bytes():
            await dg_ws.send(msg)
    except Exception:
        pass

async def _relay_dg_to_browser(dg_ws, browser_ws: WebSocket, user_cfg: dict):
    async for msg in dg_ws:
        if isinstance(msg, bytes):
            await browser_ws.send_bytes(msg)
            continue
        
        event = json.loads(msg)
        t = event.get("type", "")
        
        if t in _SIMPLE_EVENTS:
            await browser_ws.send_json({"event": _SIMPLE_EVENTS[t]})
        elif t == "ConversationText":
            await browser_ws.send_json({
                "event": "transcript",
                "role": event.get("role"),
                "text": event.get("content", ""),
            })
        elif t == "FunctionCallRequest":
            for fn in event.get("functions", []):
                await _handle_function_call(fn, dg_ws, browser_ws, user_cfg)
        elif t == "Error":
            await browser_ws.send_json({
                "event": "error",
                "message": event.get("description", "Unknown Deepgram error"),
                "code": event.get("code", ""),
            })

async def _handle_function_call(fn: dict, dg_ws, browser_ws: WebSocket, user_cfg: dict):
    name, fn_id = fn["name"], fn["id"]
    try: params = json.loads(fn.get("arguments", "{}"))
    except: params = {}
    
    if user_cfg.get("document_id"):
        params["_document_id"] = user_cfg["document_id"]
    
    result = dispatch(name, params)
    
    await browser_ws.send_json({"event": "function_call", "name": name, "params": params, "result": result})
    await dg_ws.send(json.dumps({"type": "FunctionCallResponse", "id": fn_id, "name": name, "content": json.dumps(result)}))
    
    if name in _SIDE_EFFECTS:
        await browser_ws.send_json({"event": _SIDE_EFFECTS[name], "data": result})

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        # Initial config
        first = await websocket.receive_text()
        user_cfg = json.loads(first) if first.startswith("{") else {}
        
        # Determine which API key to use:
        # 1. Custom key from the frontend
        # 2. Server's default key from .env
        client_api_key = user_cfg.get("deepgram_api_key")
        api_key = client_api_key or cfg.api_key

        if not api_key:
            await websocket.send_json({
                "event": "error", 
                "message": "Deepgram API key missing. Please provide one in the Developer settings."
            })
            await websocket.close()
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] WS connected {'(using custom key)' if client_api_key else ''}")
        
        async with websockets.connect(
            cfg.deepgram_url, 
            additional_headers={"Authorization": f"Token {api_key}"}
        ) as dg_ws:
            await dg_ws.send(json.dumps(build_settings(user_cfg)))
            
            async def _keep_alive():
                while True:
                    await asyncio.sleep(8)
                    await dg_ws.send(json.dumps({"type": "KeepAlive"}))

            await asyncio.gather(
                _relay_mic_to_dg(websocket, dg_ws),
                _relay_dg_to_browser(dg_ws, websocket, user_cfg),
                _keep_alive()
            )
    except Exception as e:
        print(f"[WS ERROR] {e}")
        try: await websocket.send_json({"event": "error", "message": str(e)})
        except: pass
    finally:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] WS disconnected")

# ── Static Files ──
# Always mount static files last to avoid overriding API routes
app.mount("/", StaticFiles(directory=str(cfg.base_dir), html=True), name="static")

def main():
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", line_buffering=True
    )
    print(f"=== Zenisth Voice Agent Server (FastAPI) ===")
    print(f"  Unified URL -> http://localhost:{cfg.http_port}")
    print(f"  API key: {'SET' if cfg.api_key else 'MISSING'}")
    uvicorn.run(app, host="0.0.0.0", port=cfg.http_port)

if __name__ == "__main__":
    main()
