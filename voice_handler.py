from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AvatarConfig,
    AzureSemanticVadMultilingual,
    AzureStandardVoice,
    ClientEventSessionAvatarConnect,
    InputAudioFormat,
    InputTextContentPart,
    InterimResponseTrigger,
    LlmInterimResponseConfig,
    MessageItem,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    VideoParams,
    VideoResolution,
)

logger = logging.getLogger(__name__)
SendMessage = Callable[[dict[str, Any]], Awaitable[None]]


class VoiceSessionHandler:
    def __init__(
        self,
        client_id: str,
        endpoint: str,
        credential: Any,
        send_message: SendMessage,
        agent_config: dict[str, str | None],
        avatar_character: str,
        avatar_style: str | None,
        voice_name: str,
        recognition_language: str,
        interim_response_latency_ms: int,
        greeting: str,
    ) -> None:
        self.client_id = client_id
        self.endpoint = endpoint
        self.credential = credential
        self.send_message = send_message
        self.agent_config = agent_config
        self.avatar_character = avatar_character
        self.avatar_style = avatar_style
        self.voice_name = voice_name
        self.recognition_language = recognition_language
        self.interim_response_latency_ms = interim_response_latency_ms
        self.greeting = greeting
        self.connection: Any = None
        self.running = False
        self.task: asyncio.Task[None] | None = None
        self.pending_greeting = False

    async def start(self) -> None:
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                api_version="2026-07-15",
                agent_name=cast(str, self.agent_config["agent_name"]),
                project_name=cast(str, self.agent_config["project_name"]),
                agent_version=self.agent_config.get("agent_version"),
                conversation_id=self.agent_config.get("conversation_id"),
                foundry_resource_override=self.agent_config.get(
                    "foundry_resource_override"
                ),
                authentication_identity_client_id=self.agent_config.get(
                    "authentication_identity_client_id"
                ),
            ) as connection:
                self.connection = connection
                await self._configure(connection)
                await self._process_events(connection)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Voice Live session failed for %s", self.client_id)
            await self.send_message({"type": "error", "message": str(exc)})
        finally:
            self.running = False
            self.connection = None

    async def _configure(self, connection: Any) -> None:
        avatar_options: dict[str, Any] = {
            "character": self.avatar_character,
            "video": VideoParams(
                codec="h264",
                resolution=VideoResolution(width=1920, height=1080),
                bitrate=1_000_000,
            ),
        }
        if self.avatar_style:
            avatar_options["style"] = self.avatar_style
        avatar = AvatarConfig(
            **avatar_options,
        )
        avatar["output_protocol"] = "webrtc"

        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=AzureStandardVoice(name=self.voice_name),
            avatar=avatar,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_transcription=AudioInputTranscriptionOptions(
                model="azure-speech",
                language=self.recognition_language,
            ),
            turn_detection=AzureSemanticVadMultilingual(),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
            interim_response=LlmInterimResponseConfig(
                triggers=[
                    InterimResponseTrigger.TOOL,
                    InterimResponseTrigger.LATENCY,
                ],
                latency_threshold_ms=self.interim_response_latency_ms,
                max_completion_tokens=30,
            ),
        )
        await connection.session.update(session=session)

    async def _process_events(self, connection: Any) -> None:
        async for event in connection:
            if not self.running:
                break
            await self._handle_event(event, connection)

    async def _handle_event(self, event: Any, connection: Any) -> None:
        event_type = event.type

        if event_type == ServerEventType.SESSION_UPDATED:
            session = event.session
            ice_servers = []
            avatar = getattr(session, "avatar", None)
            for server in getattr(avatar, "ice_servers", []) or []:
                ice_server = {"urls": server.urls}
                if server.username:
                    ice_server["username"] = server.username
                if server.credential:
                    ice_server["credential"] = server.credential
                ice_servers.append(ice_server)
            await self.send_message({"type": "ice_servers", "iceServers": ice_servers})
            await self.send_message(
                {"type": "session_started", "sessionId": getattr(session, "id", "")}
            )
            self.pending_greeting = True
        elif event_type == ServerEventType.SESSION_AVATAR_CONNECTING:
            await self.send_message(
                {"type": "avatar_sdp_answer", "serverSdp": event.server_sdp}
            )
            if self.pending_greeting:
                self.pending_greeting = False
                await connection.conversation.item.create(
                    item=MessageItem(
                        role="system",
                        content=[InputTextContentPart(text=self.greeting)],
                    )
                )
                await connection.response.create()
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            await self.send_message({"type": "status", "state": "listening"})
            await self.send_message({"type": "stop_playback"})
            try:
                await connection.response.cancel()
            except Exception:
                pass
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            await self.send_message({"type": "status", "state": "thinking"})
        elif event_type == ServerEventType.RESPONSE_CREATED:
            await self.send_message({"type": "status", "state": "speaking"})
        elif event_type == ServerEventType.RESPONSE_DONE:
            await self.send_message({"type": "status", "state": "listening"})
        elif event_type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            await self.send_message(
                {"type": "transcript", "role": "user", "text": event.transcript}
            )
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            await self.send_message(
                {"type": "transcript", "role": "assistant", "text": event.transcript}
            )
        elif event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
            await self.send_message(
                {
                    "type": "audio_data",
                    "data": base64.b64encode(event.delta).decode("ascii"),
                }
            )
        elif event_type == ServerEventType.ERROR:
            message = getattr(event.error, "message", str(event.error))
            if "no active response" not in message.lower():
                await self.send_message({"type": "error", "message": message})

    async def send_audio(self, audio: str) -> None:
        if self.connection:
            await self.connection.input_audio_buffer.append(audio=audio)

    async def send_text(self, text: str) -> None:
        if self.connection and text.strip():
            await self.connection.conversation.item.create(
                item=MessageItem(
                    role="user",
                    content=[InputTextContentPart(text=text.strip())],
                )
            )
            await self.connection.response.create()

    async def send_avatar_sdp_offer(self, client_sdp: str) -> None:
        if self.connection:
            await self.connection.send(
                ClientEventSessionAvatarConnect(client_sdp=client_sdp)
            )

    async def interrupt(self) -> None:
        if self.connection:
            try:
                await self.connection.response.cancel()
            except Exception:
                pass

    async def stop(self) -> None:
        self.running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass