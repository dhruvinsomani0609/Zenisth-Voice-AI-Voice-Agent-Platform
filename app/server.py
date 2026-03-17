import asyncio
import http
import json
import logging
import mimetypes
import time
import traceback
from pathlib import Path
from websockets.server import serve

from google import genai
from google.genai import types

from agent.settings import build_gemini_config
from agent.functions import dispatch
from config import cfg

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

HOST = cfg.server_host
PORT = cfg.server_port
MODEL = cfg.gemini_model

# Initialize Gemini Client (use explicit key so missing key fails fast)
client = genai.Client(api_key=cfg.google_api_key or None)

_INTERRUPT = object()
_STOP = object()


class _ConfigUpdate(Exception):
    def __init__(self, user_cfg: dict):
        super().__init__("config_update")
        self.user_cfg = user_cfg


async def _send_json(websocket, payload: dict) -> None:
    try:
        await websocket.send(json.dumps(payload))
    except Exception:
        # Socket might already be closing; ignore.
        return


def _drain_queue(q: asyncio.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        return


async def gemini_to_client(session, websocket):
    """Receive from Gemini and send to the browser client.

    session.receive() is a *single-turn* iterator in the google-genai SDK: it
    exhausts (breaks) as soon as Gemini sends turn_complete=True for the current
    model turn.  We therefore wrap it in an outer ``while True`` so the receiver
    immediately re-enters session.receive() after each turn completes, keeping
    the connection alive for the whole conversation.
    """
    try:
        turn = 0
        while True:
            turn += 1
            logger.info(f"Receiver: waiting for turn {turn}.")
            async for response in session.receive():
                server_content = response.server_content
                if server_content is not None:
                    model_turn = server_content.model_turn
                    if model_turn is not None:
                        for part in model_turn.parts:
                            # Forward audio chunks
                            if part.inline_data:
                                # It's an audio chunk over binary
                                await websocket.send(part.inline_data.data)

                            # Forward transcript text to the client.
                            # Skip thinking/reasoning tokens (part.thought=True) —
                            # these are Gemini's internal chain-of-thought and must
                            # never be shown to the end-user.
                            # NOTE: thinking is also suppressed at source via
                            # ThinkingConfig(include_thoughts=False) in settings.py;
                            # this guard is a defensive layer for SDK version
                            # compatibility where include_thoughts may be ignored.
                            if part.text and not part.thought:
                                await websocket.send(
                                    json.dumps({"type": "transcript", "text": part.text})
                                )

                # Check for tool calls
                tool_calls = response.tool_call
                if tool_calls and getattr(tool_calls, "function_calls", None):
                    function_responses = []
                    for function_call in tool_calls.function_calls:
                        logger.info(f"Gemini requested tool call: {function_call.name}")

                        # Execute local tool
                        kwargs = function_call.args if function_call.args else {}
                        await _send_json(
                            websocket,
                            {
                                "type": "tool_call",
                                "id": function_call.id,
                                "name": function_call.name,
                                "args": kwargs,
                            },
                        )
                        result = await dispatch(function_call.name, kwargs)
                        await _send_json(
                            websocket,
                            {
                                "type": "tool_result",
                                "id": function_call.id,
                                "name": function_call.name,
                                "result": result,
                            },
                        )

                        # Store response
                        function_responses.append(
                            types.FunctionResponse(
                                id=function_call.id,
                                name=function_call.name,
                                response=(
                                    result
                                    if isinstance(result, dict)
                                    else {"result": result}
                                ),
                            )
                        )

                    # Send the responses back to Gemini
                    if function_responses:
                        await session.send_tool_response(
                            function_responses=function_responses
                        )
    except asyncio.CancelledError:
        logger.info("Gemini to Client task cancelled")
    except Exception as e:
        logger.error(f"Error reading from Gemini: {e}")
        logger.error(traceback.format_exc())
        await _send_json(
            websocket, {"type": "error", "message": f"Gemini connection error: {e}"}
        )
    finally:
        logger.info("Gemini to Client task ended")


# Drop interrupt/config for this many seconds after client connect (avoids tear-down from stale/race messages).
_CONNECTION_GRACE_SECONDS = 5.0


async def websocket_reader(
    websocket, to_gemini_queue: asyncio.Queue, connection_start: list
):
    """Read from browser WebSocket and enqueue events for Gemini.
    connection_start: single-element list with time.monotonic() when client connected.
    Interrupt and config messages within _CONNECTION_GRACE_SECONDS are dropped so the session is not torn down.
    """
    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    elapsed = time.monotonic() - connection_start[0]
                    if msg_type == "interrupt":
                        if elapsed < _CONNECTION_GRACE_SECONDS:
                            logger.info(
                                "Dropping interrupt (within %s s of connect).",
                                _CONNECTION_GRACE_SECONDS,
                            )
                            continue
                        logger.info("Client requested interrupt. (Barge-in detected)")
                        await to_gemini_queue.put(_INTERRUPT)
                    elif msg_type == "config":
                        if elapsed < _CONNECTION_GRACE_SECONDS:
                            logger.info(
                                "Dropping config (within %s s of connect).",
                                _CONNECTION_GRACE_SECONDS,
                            )
                            continue
                        cfg_obj = data.get("config") or {}
                        await to_gemini_queue.put({"type": "config", "config": cfg_obj})
                    elif data.get("type") == "text":
                        await to_gemini_queue.put(
                            {
                                "type": "text",
                                "text": data.get("text") or "",
                            }
                        )
                    elif data.get("type") == "stop":
                        await to_gemini_queue.put(_STOP)
                except json.JSONDecodeError:
                    pass
            else:
                # Binary audio message from client (frontend uses 24kHz).
                # Use put_nowait so a backed-up queue drops frames rather than
                # blocking the reader — this keeps end-to-end latency bounded.
                try:
                    to_gemini_queue.put_nowait(
                        {
                            "type": "audio",
                            "data": message,
                            "mime_type": "audio/pcm;rate=24000",
                        }
                    )
                except asyncio.QueueFull:
                    # Drop this audio frame; the next one will arrive shortly.
                    logger.debug("Audio queue full — dropping one frame.")
    except asyncio.CancelledError:
        logger.info("WebSocket reader task cancelled")
    except Exception as e:
        logger.error(f"Error reading from client: {e}")


# Seconds after session start during which config messages are absorbed (no reconnect)
_CONFIG_GRACE_SECONDS = 10.0


async def gemini_sender(
    session, to_gemini_queue: asyncio.Queue, user_cfg: dict, session_start: list
):
    """Consume queued events and send them to Gemini.
    session_start: single-element list with monotonic time when session started.
    Config messages within CONFIG_GRACE_SECONDS are absorbed; after that they trigger reconnect.
    """
    while True:
        item = await to_gemini_queue.get()
        if item is _INTERRUPT:
            logger.info("Sender: interrupt received.")
            raise asyncio.CancelledError()
        if item is _STOP:
            raise asyncio.CancelledError()

        if isinstance(item, dict) and item.get("type") == "config":
            cfg_obj = item.get("config") or {}
            elapsed = time.monotonic() - session_start[0]
            if elapsed < _CONFIG_GRACE_SECONDS:
                # Within grace window — apply in memory only, never reconnect
                user_cfg.clear()
                user_cfg.update(cfg_obj)
                logger.info(
                    "Config applied in memory (within grace window, no reconnect)."
                )
                continue
            logger.info("Config update after grace window; triggering reconnect.")
            raise _ConfigUpdate(cfg_obj)

        if isinstance(item, dict) and item.get("type") == "text":
            text_val = item.get("text") or ""
            logger.info(f"Sending text to Gemini: {text_val}")
            # send_realtime_input(text=...) is the correct 1.0+ SDK path for plain
            # text turns; audio uses the audio= parameter with an explicit MIME type.
            await session.send_realtime_input(text=text_val)
        elif isinstance(item, dict) and item.get("type") == "audio":
            # The `audio` keyword is the correct parameter in google-genai 1.x
            # (`media_chunks` does not exist in this SDK version).
            await session.send_realtime_input(
                audio=types.Blob(
                    data=item["data"],
                    # Explicit sample rate so Gemini knows the incoming PCM
                    # format matches the 24 kHz AudioContext on the client.
                    mime_type="audio/pcm;rate=24000",
                )
            )


async def handle_client(websocket):
    """Handle a single client WebSocket connection."""
    logger.info(f"Client connected: {websocket.remote_address}")

    user_cfg: dict = {}
    live_config = build_gemini_config(user_cfg=user_cfg)

    try:
        await _send_json(websocket, {"type": "status", "state": "connecting"})

        to_gemini_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        connection_start = [time.monotonic()]

        # Start the WebSocket reader once; we keep it across Gemini session restarts.
        reader_task = asyncio.create_task(
            websocket_reader(websocket, to_gemini_queue, connection_start)
        )

        while not reader_task.done():
            # Establish bi-directional stream with Gemini Multimodal Live API
            async with client.aio.live.connect(
                model=MODEL, config=live_config
            ) as session:
                logger.info("Connected to Gemini Live API.")
                await _send_json(websocket, {"type": "status", "state": "live"})
                await _send_json(
                    websocket, {"type": "system", "message": "Connected to AI agent."}
                )

                # Trigger the agent to deliver its opening greeting immediately
                # so the user hears the agent speak first without having to say
                # anything themselves.
                await session.send_realtime_input(
                    text="[Call connected. Please deliver your opening greeting now.]"
                )

                session_start = [time.monotonic()]
                sender_task = asyncio.create_task(
                    gemini_sender(session, to_gemini_queue, user_cfg, session_start)
                )
                receiver_task = asyncio.create_task(
                    gemini_to_client(session, websocket)
                )

                done, pending = await asyncio.wait(
                    {reader_task, sender_task, receiver_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if reader_task in done:
                    logger.info("Session End: Browser WebSocket closed.")
                    for t in pending:
                        t.cancel()
                    break
                elif receiver_task in done:
                    logger.info("Session End: Gemini session loop finished.")
                    for t in pending:
                        t.cancel()
                    break
                elif sender_task in done:
                    if sender_task.cancelled():
                        logger.info("Session Reset: Interrupt/Stop requested.")
                        await _send_json(websocket, {"type": "reset_playback"})
                        _drain_queue(to_gemini_queue)
                    else:
                        exc = sender_task.exception()
                        if isinstance(exc, _ConfigUpdate):
                            logger.info("Session Re-config: Updating settings.")
                            user_cfg.clear()
                            user_cfg.update(exc.user_cfg or {})
                            live_config = build_gemini_config(user_cfg=user_cfg)
                            await _send_json(
                                websocket,
                                {"type": "system", "message": "Settings applied."},
                            )
                            await _send_json(websocket, {"type": "reset_playback"})
                            _drain_queue(to_gemini_queue)
                        else:
                            logger.error(f"Session Error: Sender task failed: {exc}")
                            if exc:
                                logger.error(
                                    "".join(
                                        traceback.format_exception(
                                            type(exc), exc, exc.__traceback__
                                        )
                                    )
                                )
                            for t in pending:
                                t.cancel()
                            break

                # Cancel receiver (Gemini->client) before reopening a new session.
                receiver_task.cancel()
                try:
                    await receiver_task
                except Exception:
                    pass

    except Exception as e:
        err_msg = str(e).strip() or "Unknown error"
        logger.error(f"Failed to connect to Gemini: {e}")
        logger.error(traceback.format_exc())
        await _send_json(
            websocket,
            {"type": "error", "message": f"Failed to connect to AI: {err_msg}"},
        )
    finally:
        logger.info(f"Client disconnected: {websocket.remote_address}")


# ── Static-file serving ────────────────────────────────────────────────────────
# Serve the browser frontend (index.html, script.js, …) from the SAME server
# and port as the WebSocket endpoint.  This means the app works with a single
# forwarded port — critical for GitHub Codespaces and other reverse-proxy
# environments where different ports have completely different hostnames.

_STATIC_DIR: Path = cfg.base_dir  # project root — where index.html lives


async def _process_http_request(path: str, request_headers):
    """Serve static files for plain HTTP requests.

    Returns None to let websockets proceed with the WebSocket upgrade handshake.
    Returns an HTTP 3-tuple (status, headers, body) for regular GET requests so
    the frontend files are served from the same port as the WebSocket endpoint.
    """
    # WebSocket upgrade — let the library handle it normally.
    if request_headers.get("Upgrade", "").lower() == "websocket":
        return None

    # Strip query string and fragment; default to index.html.
    clean = path.split("?")[0].split("#")[0]
    if clean in ("", "/"):
        clean = "/index.html"

    candidate = (_STATIC_DIR / clean.lstrip("/")).resolve()

    # Security: reject traversal attempts outside the static root.
    try:
        candidate.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return http.HTTPStatus.FORBIDDEN, [], b"Forbidden"

    try:
        data = candidate.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return http.HTTPStatus.NOT_FOUND, [], b"Not Found"

    mime, _ = mimetypes.guess_type(str(candidate))
    return (
        http.HTTPStatus.OK,
        [
            ("Content-Type", mime or "application/octet-stream"),
            ("Content-Length", str(len(data))),
        ],
        data,
    )


async def main():
    logger.info(f"Starting S2S Server on {HOST}:{PORT}")
    async with serve(
        handle_client, HOST, PORT,
        process_request=_process_http_request,
        ping_timeout=60,
    ):
        logger.info(f"Open browser at: http://127.0.0.1:{PORT}")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    asyncio.run(main())
