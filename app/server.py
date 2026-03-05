import asyncio
import io
import json
import os
import sys
import threading
import http.server
import yaml
from datetime import datetime
from pathlib import Path

import websockets

from app.config import cfg
from app.agent.settings import build_settings
from app.agent.functions import dispatch

UPLOADS_DIR = cfg.base_dir / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

_UPLOADS_INDEX = UPLOADS_DIR / "index.json"


def _load_uploads_index() -> dict:
    """Load uploads/index.json → {filename: {document_id, uploaded_at, file_size}}."""
    if _UPLOADS_INDEX.exists():
        try:
            return json.loads(_UPLOADS_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _update_uploads_index(doc_id: str, filename: str, file_size: int) -> None:
    """Add or update an entry in uploads/index.json."""
    index = _load_uploads_index()
    index[filename] = {
        "document_id": doc_id,
        "file_size": file_size,
        "uploaded_at": datetime.now().isoformat(),
    }
    _UPLOADS_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _remove_from_uploads_index(filename: str) -> None:
    """Remove a filename entry from uploads/index.json."""
    index = _load_uploads_index()
    if filename in index:
        del index[filename]
        _UPLOADS_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


# ── Deepgram event type → browser event name ──────────────────────────────────
_SIMPLE_EVENTS: dict[str, str] = {
    "SettingsApplied": "connected",
    "AgentThinking": "agent_thinking",
    "AgentStartedSpeaking": "agent_speaking",
    "AgentAudioDone": "agent_done",
    "UserStartedSpeaking": "user_speaking",
}

# function name → browser side-effect event name
_SIDE_EFFECTS: dict[str, str] = {
    "book_appointment": "appointment_booked",
    "mark_dnc": "dnc_marked",
    "transfer_to_human": "transfer_initiated",
    "end_call": "call_ended",
}


# ── HTTP handler ───────────────────────────────────────────────────────────────
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(cfg.base_dir), **kwargs)

    def log_message(self, fmt, *args):
        pass  # suppress per-request noise

    # ── GET routes ──────────────────────────────────────────────────────────────
    def do_GET(self):
        match self.path:
            case "/api/models":
                self._send_json(cfg.models)
            case "/api/config":
                self._send_json(cfg.defaults)
            case "/api/documents":
                self._handle_list_documents()
            case _:
                super().do_GET()

    # ── POST routes ─────────────────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/api/upload":
            self._handle_upload()
        elif self.path == "/api/activate":
            self._handle_activate()
        elif self.path == "/api/config/save":
            self._handle_config_save()
        else:
            self.send_response(404)
            self.end_headers()

    # ── DELETE routes ────────────────────────────────────────────────────────────
    def do_DELETE(self):
        if self.path.startswith("/api/documents"):
            self._handle_delete_document()
        else:
            self.send_response(404)
            self.end_headers()

    # ── Handlers ─────────────────────────────────────────────────────────────────
    def _handle_upload(self):
        """Accept multipart/form-data file upload (PDF/DOCX/TXT), run RAG ingestion pipeline."""
        import re as _re

        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._send_json({"error": "Expected multipart/form-data"}, 400)
                return

            body_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(body_len)

            # ── Manual multipart parser ── avoids Python 3.12 cgi bool bug ───────────
            boundary = None
            for seg in content_type.split(";"):
                seg = seg.strip()
                if seg.lower().startswith("boundary="):
                    boundary = seg[len("boundary=") :].strip().strip('"')
                    break
            if not boundary:
                self._send_json({"error": "Missing boundary in Content-Type"}, 400)
                return

            sep = ("--" + boundary).encode()
            file_bytes: bytes | None = None
            filename: str | None = None

            for chunk in raw_body.split(sep):
                chunk = chunk.lstrip(b"\r\n")
                if not chunk or chunk.startswith(b"--"):
                    continue
                if b"\r\n\r\n" not in chunk:
                    continue
                hdr_raw, _, body = chunk.partition(b"\r\n\r\n")
                body = body.rstrip(b"\r\n")
                hdr_text = hdr_raw.decode("utf-8", errors="replace")

                disposition = ""
                for hline in hdr_text.splitlines():
                    if hline.lower().startswith("content-disposition"):
                        disposition = hline.partition(":")[2].strip()
                        break

                params: dict[str, str] = {}
                for tok in disposition.split(";"):
                    tok = tok.strip()
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        params[k.strip().lower()] = v.strip().strip('"')

                field = params.get("name", "")
                if field == "file":
                    filename = params.get("filename", "upload.bin")
                    file_bytes = body

            if not file_bytes or not filename:
                self._send_json({"error": "No file field found in upload"}, 400)
                return

            filename = Path(filename).name
            ext = Path(filename).suffix.lower()
            if ext not in {".pdf", ".docx", ".txt"}:
                self._send_json(
                    {"error": f"Unsupported type '{ext}'. Allowed: PDF, DOCX, TXT"}, 400
                )
                return

            # Derive doc_id from filename (sanitise to lowercase underscores)
            doc_id = _re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")

            # Save file to disk
            dest = UPLOADS_DIR / filename
            dest.write_bytes(file_bytes)
            file_size = len(file_bytes)
            print(f"[upload] Saved {file_size:,} bytes -> {dest} | doc_id='{doc_id}'")

            # Persist to uploads index
            _update_uploads_index(doc_id, filename, file_size)

            self._send_json(
                {
                    "status": "uploaded",
                    "document_id": doc_id,
                    "filename": filename,
                    "file_size": file_size,
                }
            )

        except Exception as exc:
            import traceback

            traceback.print_exc()
            self._send_json({"error": str(exc)}, 500)

    def _handle_activate(self):
        """Ingest a previously uploaded file and mark it as the active document."""
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(body_len)) if body_len else {}
            doc_id = body.get("document_id", "").strip()
            filename = body.get("filename", "").strip()
            if not doc_id or not filename:
                self._send_json({"error": "document_id and filename are required"}, 400)
                return

            dest = UPLOADS_DIR / filename
            if not dest.exists():
                self._send_json({"error": f"File not found: {filename}"}, 404)
                return

            result = {"status": "ok", "document_id": doc_id}
            err = [None]

            def _ingest():
                try:
                    import asyncio as _aio
                    from app.services.ingestion import ingest_document
                    from app.services.storage import (
                        insert_nodes,
                        cache_document_outline,
                    )
                    from supabase._async.client import create_client as _cc

                    loop = _aio.new_event_loop()
                    _aio.set_event_loop(loop)

                    # Check if already ingested
                    async def _check_existing():
                        url = os.environ.get("SUPABASE_URL", "").strip()
                        key = os.environ.get("SUPABASE_KEY", "").strip()
                        if not url or not key:
                            return 0
                        client = await _cc(url, key)
                        resp = (
                            await client.table("document_nodes")
                            .select("node_id", count="exact")
                            .eq("document_id", doc_id)
                            .limit(1)
                            .execute()
                        )
                        return resp.count or len(resp.data)

                    existing = loop.run_until_complete(_check_existing())
                    if existing:
                        print(
                            f"[activate] '{doc_id}' already ingested ({existing} nodes)"
                        )
                        result["node_count"] = existing
                        result["status"] = "already_ingested"
                        loop.close()
                        return

                    print(f"[activate] Ingesting '{dest}' as doc_id='{doc_id}'")
                    nodes = loop.run_until_complete(ingest_document(str(dest), doc_id))
                    print(f"[activate] Parsed {len(nodes)} nodes")
                    loop.run_until_complete(insert_nodes(nodes))
                    print(f"[activate] Nodes stored in Supabase")
                    loop.run_until_complete(cache_document_outline(doc_id))
                    print(f"[activate] Outline cached in Redis")
                    result["node_count"] = len(nodes)
                    loop.close()
                except Exception as exc:
                    import traceback

                    print(f"[activate] ERROR: {exc}")
                    traceback.print_exc()
                    err[0] = str(exc)

            t = threading.Thread(target=_ingest)
            t.start()
            t.join(timeout=180)  # allow up to 3 min for large docs

            if err[0]:
                self._send_json({"error": err[0]}, 500)
            else:
                self._send_json(result)

        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_list_documents(self):
        """
        Unified document list — index-driven (production-level):
          1. Load uploads/index.json — the single source of truth for registered docs.
             Only files explicitly uploaded via /api/upload appear here.
             Pre-existing files in uploads/ that were never uploaded through the UI
             are intentionally invisible, preventing phantom "default" documents.
          2. Query Supabase for ingested doc_ids + node counts.
          3. Merge — each file shows status: 'uploaded' | 'ready'.
        """
        try:
            import asyncio as _aio
            from supabase._async.client import create_client

            # ── 1. Build file list from index.json only ─────────────────────
            # We deliberately do NOT scan the uploads/ directory directly.
            # Only documents that were explicitly registered via /api/upload
            # (written into index.json) are included. This prevents leftover
            # files or pre-placed files from appearing in the library on restart.
            index = _load_uploads_index()
            disk_files: list[dict] = []

            for fname, meta in index.items():
                file_path = UPLOADS_DIR / fname
                # Skip if the physical file was manually deleted from disk
                if not file_path.exists():
                    continue
                stat = file_path.stat()
                doc_id = meta.get("document_id", "")
                if not doc_id:
                    import re as _re

                    doc_id = _re.sub(
                        r"[^a-z0-9]+", "_", Path(fname).stem.lower()
                    ).strip("_")
                disk_files.append(
                    {
                        "document_id": doc_id,
                        "filename": fname,
                        "file_size": stat.st_size,
                        "modified_at": meta.get(
                            "uploaded_at",
                            datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        ),
                        "status": "uploaded",  # default; overwritten if ingested
                        "node_count": 0,
                        "created_at": "",
                    }
                )

            # Sort newest first (by uploaded_at from index)
            disk_files.sort(key=lambda x: x["modified_at"], reverse=True)

            # ── 2. Query Supabase for ingested documents ─────────────────────
            async def _fetch_ingested():
                url = os.environ.get("SUPABASE_URL", "").strip()
                key = os.environ.get("SUPABASE_KEY", "").strip()
                if not url or not key:
                    return {}
                client = await create_client(url, key)
                resp = (
                    await client.table("document_nodes")
                    .select("document_id, created_at")
                    .execute()
                )
                ingested: dict[str, dict] = {}
                for row in resp.data:
                    did = row["document_id"]
                    if did not in ingested:
                        ingested[did] = {
                            "node_count": 0,
                            "created_at": row.get("created_at", ""),
                        }
                    ingested[did]["node_count"] += 1
                return ingested

            loop = _aio.new_event_loop()
            ingested_map = loop.run_until_complete(_fetch_ingested())
            loop.close()

            # ── 3. Merge ───────────────────────────────────────────────
            # Also include ingested docs that may not be in uploads/ (e.g. CLI ingested)
            seen_doc_ids = {entry["document_id"] for entry in disk_files}
            for did, info in ingested_map.items():
                if did not in seen_doc_ids:
                    disk_files.append(
                        {
                            "document_id": did,
                            "filename": did + " (file not on disk)",
                            "file_size": 0,
                            "modified_at": info["created_at"],
                            "status": "ready",
                            "node_count": info["node_count"],
                            "created_at": info["created_at"],
                        }
                    )

            for entry in disk_files:
                did = entry["document_id"]
                if did in ingested_map:
                    entry["status"] = "ready"
                    entry["node_count"] = ingested_map[did]["node_count"]
                    entry["created_at"] = ingested_map[did]["created_at"]

            self._send_json(disk_files)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_delete_document(self):
        """Delete document from Supabase, Redis, and uploads/ directory."""
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(body_len)) if body_len else {}
            doc_id = body.get("document_id", "").strip()
            filename = body.get("filename", "").strip()
            if not doc_id:
                self._send_json({"error": "document_id required"}, 400)
                return

            import asyncio as _aio

            async def _delete():
                from supabase._async.client import create_client
                import redis.asyncio as aioredis

                url = os.environ.get("SUPABASE_URL", "").strip()
                key = os.environ.get("SUPABASE_KEY", "").strip()
                client = await create_client(url, key)
                await client.table("document_nodes").delete().eq(
                    "document_id", doc_id
                ).execute()
                redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
                r = await aioredis.from_url(redis_url)
                await r.delete(f"outline:{doc_id}")
                await r.aclose()

            loop = _aio.new_event_loop()
            loop.run_until_complete(_delete())
            loop.close()

            # Also delete the physical file from uploads/
            if filename:
                file_path = UPLOADS_DIR / filename
                if file_path.exists():
                    file_path.unlink()
                    print(f"[delete] Removed file: {file_path}")
                # Clean up from index
                _remove_from_uploads_index(filename)

            self._send_json({"status": "deleted", "document_id": doc_id})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_config_save(self):
        """Persist updated config values to config.yaml and reload cfg."""
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            updates = json.loads(self.rfile.read(body_len)) if body_len else {}
            config_path = cfg.base_dir / "config.yaml"
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)

            agent = raw.setdefault("agent", {})
            defaults = agent.setdefault("defaults", {})

            allowed = {
                "voice_model",
                "stt_model",
                "llm_model",
                "temperature",
                "greeting",
            }
            for k, v in updates.items():
                if k in allowed:
                    defaults[k] = v
                elif k == "system_prompt":
                    # Store system prompt in a sidecar file
                    (cfg.base_dir / "system_prompt.txt").write_text(v, encoding="utf-8")

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)

            # Hot-reload cfg
            cfg._raw = raw
            self._send_json({"status": "saved"})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def _run_http_server():
    httpd = http.server.HTTPServer(("0.0.0.0", cfg.http_port), _QuietHandler)
    httpd.serve_forever()


# ── WebSocket relay ────────────────────────────────────────────────────────────
async def _relay_mic_to_dg(browser_ws, dg_ws):
    """Forward binary audio chunks from browser to Deepgram."""
    count = 0
    async for msg in browser_ws:
        if isinstance(msg, bytes):
            await dg_ws.send(msg)
            count += 1
            if count % 100 == 0:
                print(f"  [relay] forwarded {count} chunks of mic audio")
        else:
            # Handle string messages (like end_call)
            try:
                data = json.loads(msg)
                if data.get("type") == "end_call":
                    print("  [relay] received end_call from browser")
                    break
            except Exception:
                pass


async def _keep_alive(dg_ws):
    """Deepgram Agent API requires a KeepAlive message every ~10s or it closes connection."""
    while True:
        try:
            await asyncio.sleep(8)
            await dg_ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            break


async def _relay_dg_to_browser(dg_ws, browser_ws, user_cfg: dict):
    """Translate Deepgram events → browser events; execute function calls inline."""
    async for msg in dg_ws:
        if isinstance(msg, bytes):
            await browser_ws.send(msg)  # raw TTS audio — forward directly
            continue

        event = json.loads(msg)
        t = event.get("type", "")

        # Simple 1-to-1 event mappings
        if t in _SIMPLE_EVENTS:
            await browser_ws.send(json.dumps({"event": _SIMPLE_EVENTS[t]}))

        elif t == "ConversationText":
            await browser_ws.send(
                json.dumps(
                    {
                        "event": "transcript",
                        "role": event.get("role"),
                        "text": event.get("content", ""),
                    }
                )
            )

        elif t == "FunctionCallRequest":
            for fn in event.get("functions", []):
                await _handle_function_call(fn, dg_ws, browser_ws, user_cfg)

        elif t == "Error":
            await browser_ws.send(
                json.dumps(
                    {
                        "event": "error",
                        "message": event.get("description", "Unknown Deepgram error"),
                        "code": event.get("code", ""),
                    }
                )
            )


async def _handle_function_call(fn: dict, dg_ws, browser_ws, user_cfg: dict):
    name = fn["name"]
    fn_id = fn["id"]
    try:
        params = json.loads(fn.get("arguments", "{}"))
    except Exception:
        params = {}

    # Inject document_id if the frontend provided it
    if user_cfg and "document_id" in user_cfg and user_cfg["document_id"]:
        params["_document_id"] = user_cfg["document_id"]

    result = dispatch(name, params)

    # Notify browser about the function call + result
    await browser_ws.send(
        json.dumps(
            {
                "event": "function_call",
                "name": name,
                "params": params,
                "result": result,
            }
        )
    )

    # Return result to Deepgram
    await dg_ws.send(
        json.dumps(
            {
                "type": "FunctionCallResponse",
                "id": fn_id,
                "name": name,
                "content": json.dumps(result),
            }
        )
    )

    # Emit side-effect event (toast in browser) if applicable
    if name in _SIDE_EFFECTS:
        await browser_ws.send(
            json.dumps(
                {
                    "event": _SIDE_EFFECTS[name],
                    "data": result,
                }
            )
        )


async def handle_browser_client(browser_ws):
    if not cfg.api_key:
        await browser_ws.send(
            json.dumps(
                {
                    "event": "error",
                    "message": "DEEPGRAM_API_KEY is not set. Set it and restart the server.",
                }
            )
        )
        return

    # First message from browser must be {type: "config", ...}
    user_cfg: dict = {}
    try:
        first = await asyncio.wait_for(browser_ws.recv(), timeout=8.0)
        if isinstance(first, str):
            data = json.loads(first)
            if data.get("type") == "config":
                user_cfg = data
    except Exception:
        pass  # connect with defaults if config not received in time

    voice = user_cfg.get("voice_model") or cfg.defaults["voice_model"]
    stt = user_cfg.get("stt_model") or cfg.defaults["stt_model"]
    llm = user_cfg.get("llm_model") or cfg.defaults["llm_model"]
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] connected | voice={voice} stt={stt} llm={llm}"
    )

    try:
        async with websockets.connect(
            cfg.deepgram_url,
            additional_headers={"Authorization": f"Token {cfg.api_key}"},
        ) as dg_ws:
            await dg_ws.send(json.dumps(build_settings(user_cfg)))
            await asyncio.gather(
                _relay_mic_to_dg(browser_ws, dg_ws),
                _relay_dg_to_browser(dg_ws, browser_ws, user_cfg),
                _keep_alive(dg_ws),
            )
    except Exception as exc:
        print(f"[ERROR] {exc}")
        with open("server_error.log", "a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat()} | {exc}\n")
        try:
            await browser_ws.send(json.dumps({"event": "error", "message": str(exc)}))
        except Exception:
            pass
    finally:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] disconnected")


# ── Entry point ────────────────────────────────────────────────────────────────
async def main():
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", line_buffering=True
    )

    threading.Thread(target=_run_http_server, daemon=True).start()

    print("=== Zenisth Voice Agent Server ===")
    print(f"  HTTP  -> http://localhost:{cfg.http_port}")
    print(f"  WS    -> ws://localhost:{cfg.ws_port}")
    print(f"  API key: {'SET' if cfg.api_key else 'MISSING'}")
    print()

    async with websockets.serve(handle_browser_client, "0.0.0.0", cfg.ws_port):
        await asyncio.Future()
