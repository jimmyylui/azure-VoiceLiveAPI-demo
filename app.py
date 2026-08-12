from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from voice_handler import VoiceSessionHandler

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
sessions: dict[str, VoiceSessionHandler] = {}
credential: DefaultAzureCredential | None = None
MAX_AUDIO_CHUNK_LENGTH = 16_000
MAX_SDP_LENGTH = 512_000
MAX_TEXT_LENGTH = 4_000
MESSAGE_TYPES = {
    "audio_chunk",
    "avatar_sdp_offer",
    "interrupt",
    "send_text",
    "start_session",
    "stop_session",
}


def required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def build_agent_config(conversation_id: str | None = None) -> dict[str, str | None]:
    resource_override = os.getenv("FOUNDRY_RESOURCE_OVERRIDE") or None
    return {
        "agent_name": required_setting("AGENT_NAME"),
        "project_name": required_setting("PROJECT_NAME"),
        "agent_version": os.getenv("AGENT_VERSION") or None,
        "conversation_id": conversation_id or os.getenv("CONVERSATION_ID") or None,
        "foundry_resource_override": resource_override,
        "authentication_identity_client_id": (
            os.getenv("AGENT_AUTHENTICATION_IDENTITY_CLIENT_ID") or None
            if resource_override
            else None
        ),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    global credential
    credential = DefaultAzureCredential()
    yield
    for handler in list(sessions.values()):
        await handler.stop()
    sessions.clear()
    await credential.close()


app = FastAPI(title="Foundry Agent Avatar", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    missing = [name for name in ("VOICELIVE_ENDPOINT", "AGENT_NAME", "PROJECT_NAME") if not os.getenv(name)]
    return {
        "ready": not missing,
        "missing": missing,
        "agentName": os.getenv("AGENT_NAME", "Foundry Agent"),
        "avatarCharacter": os.getenv("AVATAR_CHARACTER", "lisa"),
    }


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str) -> None:
    client_id = "".join(character for character in client_id if character.isalnum() or character in "-_")[:80]
    await websocket.accept()

    async def send(message: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(message))

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if not isinstance(message, dict):
                raise ValueError("WebSocket messages must be JSON objects")
            message_type = message.get("type")
            if message_type not in MESSAGE_TYPES:
                raise ValueError("Unsupported WebSocket message type")
            handler = sessions.get(client_id)

            if message_type == "start_session":
                conversation_id = message.get("conversationId")
                if conversation_id is not None and (
                    not isinstance(conversation_id, str) or len(conversation_id) > 200
                ):
                    raise ValueError("Invalid conversation ID")
                if handler:
                    await handler.stop()
                if credential is None:
                    raise RuntimeError("Credential is not initialized")
                handler = VoiceSessionHandler(
                    client_id=client_id,
                    endpoint=required_setting("VOICELIVE_ENDPOINT"),
                    credential=credential,
                    send_message=send,
                    agent_config=build_agent_config(conversation_id),
                    avatar_character=os.getenv("AVATAR_CHARACTER", "lisa"),
                    avatar_style=os.getenv("AVATAR_STYLE", "casual-sitting") or None,
                    voice_name=os.getenv("VOICE_NAME", "zh-HK-HiuMaanNeural"),
                    recognition_language=os.getenv(
                        "SPEECH_RECOGNITION_LANGUAGE", "zh-HK"
                    ),
                    interim_response_latency_ms=int(
                        os.getenv("INTERIM_RESPONSE_LATENCY_MS", "800")
                    ),
                    greeting=os.getenv(
                        "GREETING_PROMPT",
                        "Welcome the user briefly and ask how you can help.",
                    ),
                )
                sessions[client_id] = handler
                await handler.start()
            elif message_type == "audio_chunk" and handler:
                audio = message.get("data")
                if not isinstance(audio, str) or len(audio) > MAX_AUDIO_CHUNK_LENGTH:
                    raise ValueError("Invalid audio chunk")
                await handler.send_audio(audio)
            elif message_type == "avatar_sdp_offer" and handler:
                client_sdp = message.get("clientSdp")
                if not isinstance(client_sdp, str) or len(client_sdp) > MAX_SDP_LENGTH:
                    raise ValueError("Invalid avatar SDP offer")
                await handler.send_avatar_sdp_offer(client_sdp)
            elif message_type == "send_text" and handler:
                text = message.get("text")
                if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
                    raise ValueError("Invalid text message")
                await handler.send_text(text)
            elif message_type == "interrupt" and handler:
                await handler.interrupt()
            elif message_type == "stop_session":
                if handler:
                    await handler.stop()
                    sessions.pop(client_id, None)
                await send({"type": "session_stopped"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("WebSocket failure for %s", client_id)
        await send({"type": "error", "message": str(exc)})
    finally:
        handler = sessions.pop(client_id, None)
        if handler:
            await handler.stop()


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("app:app", host="localhost", port=8000, reload=True)
