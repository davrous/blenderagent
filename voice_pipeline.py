"""Voice pipeline for the hosted Blender Scene Agent.

This module adds a *speech-in / speech-out* path alongside the normal text
Responses API. It is entirely self-contained and optional: if speech is not
configured (or ``ENABLE_VOICE`` is off) the agent keeps working as a text-only
agent and this module never touches the request path.

Architecture
------------
* A dedicated WebSocket server (``invocations_ws`` protocol, default port 8089)
  accepts a browser voice connection relayed by the webchat server.
* Inbound **binary** frames are 24 kHz / 16-bit / mono PCM microphone audio.
  They are pushed into Azure Speech STT (continuous recognition).
* Inbound **text** frames are small JSON control messages:
  ``start`` (begin capturing), ``commit`` (stop + run the turn), ``cancel``
  (barge-in), ``context`` (optional continuity hint).
* On ``commit`` the recognised transcript is sent to the agent's *local*
  ``/responses`` endpoint (same container), the streamed reply is forwarded to
  the browser as ``delta`` frames (identical text to the typed path, including
  the inline ``*status*`` markers the UI turns into pills) and the speakable
  prose is synthesised to 24 kHz PCM and streamed back as binary frames.

Blender-specific behaviour
---------------------------
* The Blender scene lives **server-side**, keyed by ``conversation_id``. The
  browser therefore sends ``conversation_id`` (and the client-owned
  ``previous_response_id``) in the control frames, and we forward them on the
  local ``/responses`` call so :class:`SceneIsolationMiddleware` activates and
  saves the *same* scene the typed chat uses. Voice and text share one scene.
* Blender replies embed markdown **images** (``![screenshot](url)``) and
  **download links** (``[Download …](url)``) rather than code blocks. These are
  stripped from the spoken audio (URLs are never read aloud) but still travel to
  the browser inside the ``delta``/``done`` text for rendering. When a reply
  contains a screenshot/render or a download link we always speak a short spoken
  cue so the user is told something visual/downloadable is ready.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger("blender_agent.voice")

try:
    from opentelemetry import context as otel_context
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Status, StatusCode

    _tracer = otel_trace.get_tracer("blender_agent.voice")
    _meter = otel_metrics.get_meter("blender_agent.voice")
    _frames_received = _meter.create_counter("websocket.frames.received")
    _frames_sent = _meter.create_counter("websocket.frames.sent")
    _bytes_received = _meter.create_counter("websocket.frames.bytes_received")
    _bytes_sent = _meter.create_counter("websocket.frames.bytes_sent")
    _stage_failures = _meter.create_counter("voice.stage.failures")
    _stt_duration = _meter.create_histogram("voice.stt.duration_ms", unit="ms")
    _stt_finalize_duration = _meter.create_histogram(
        "voice.stt.finalization.duration_ms", unit="ms"
    )
    _agent_duration = _meter.create_histogram("voice.agent.duration_ms", unit="ms")
    _agent_first_delta = _meter.create_histogram(
        "voice.agent.time_to_first_delta_ms", unit="ms"
    )
    _tts_duration = _meter.create_histogram("voice.tts.duration_ms", unit="ms")
    _turn_duration = _meter.create_histogram("voice.turn.duration_ms", unit="ms")
    _turn_first_audio = _meter.create_histogram(
        "voice.turn.time_to_first_audio_ms", unit="ms"
    )
except ImportError:  # Local text-only environments may omit OTel.
    otel_context = None
    otel_trace = None
    Status = StatusCode = None
    _tracer = None
    _frames_received = _frames_sent = None
    _bytes_received = _bytes_sent = None
    _stage_failures = None
    _stt_duration = _stt_finalize_duration = None
    _agent_duration = _agent_first_delta = None
    _tts_duration = _turn_duration = _turn_first_audio = None


def _span(name: str, attributes: Optional[dict[str, Any]] = None):
    if _tracer is None:
        return contextlib.nullcontext(None)
    return _tracer.start_as_current_span(name, attributes=attributes or {})


def _record(instrument: Any, value: float | int, attributes: Optional[dict[str, Any]] = None) -> None:
    if instrument is not None:
        instrument.record(value, attributes or {})


def _count(instrument: Any, value: int, attributes: Optional[dict[str, Any]] = None) -> None:
    if instrument is not None:
        instrument.add(value, attributes or {})


def _record_failure(stage: str, exc: BaseException | None = None, code: str | None = None) -> None:
    attributes = {"voice.stage": stage}
    if exc is not None:
        attributes["error.type"] = type(exc).__name__
    if code:
        attributes["error.code"] = code[:80]
    _count(_stage_failures, 1, attributes)


def _set_span_error(span: Any, exc: BaseException | None = None, code: str | None = None) -> None:
    if span is None or Status is None or StatusCode is None:
        return
    if exc is not None:
        span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, code or (str(exc)[:200] if exc else "error")))

# ── Audio format ──────────────────────────────────────────────────────────
SAMPLE_RATE = 24000
BITS_PER_SAMPLE = 16
CHANNELS = 1
# 20 ms of PCM audio at the above format = 24000 * 2 bytes * 1 channel * 0.02s.
TTS_FRAME_BYTES = int(SAMPLE_RATE * (BITS_PER_SAMPLE // 8) * CHANNELS * 0.02)

# ── WebSocket transport ───────────────────────────────────────────────────
VOICE_WS_PORT = int(os.environ.get("VOICE_WS_PORT", "8089"))
VOICE_WS_PATH = "/invocations_ws"

# ── Speech synthesis voices ───────────────────────────────────────────────
SPEECH_VOICE_NAME = os.environ.get(
    "SPEECH_VOICE_NAME", "en-US-NovaMultilingualNeuralHD"
)
SPEECH_VOICE_FALLBACK = os.environ.get(
    "SPEECH_VOICE_FALLBACK", "en-US-AvaMultilingualNeural"
)
SPEECH_RECOGNITION_LANGUAGE = os.environ.get("SPEECH_RECOGNITION_LANGUAGE", "en-US")

# ── Progress narration timing (fills silent gaps while tools run) ─────────
PROGRESS_FIRST_MS = int(os.environ.get("VOICE_PROGRESS_FIRST_MS", "900"))
PROGRESS_INTERVAL_MS = int(os.environ.get("VOICE_PROGRESS_INTERVAL_MS", "4500"))

# ── Azure auth for Speech (keyless / AAD) ─────────────────────────────────
SPEECH_AAD_SCOPE = "https://cognitiveservices.azure.com/.default"
FOUNDRY_CALL_ID_HEADER = "x-agent-foundry-call-id"
FOUNDRY_USER_ID_HEADER = "x-agent-user-id"

# ── Agent local Responses endpoint (same container) ───────────────────────
SERVER_PORT = int(os.environ.get("PORT", "8088"))
LOCAL_RESPONSES_URL = f"http://localhost:{SERVER_PORT}/responses"
# The model field is largely cosmetic for the local ResponsesHostServer (it
# resolves the real deployment from the agent), but we mirror the deployment
# name the typed chat uses for consistency.
AGENT_MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4.1")


# ── Availability checks ────────────────────────────────────────────────────
def _voice_enabled() -> bool:
    return os.environ.get("ENABLE_VOICE", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _speech_configured() -> bool:
    """True when Speech is reachable via key, endpoint, or region + AAD."""
    key = os.environ.get("SPEECH_KEY")
    region = os.environ.get("SPEECH_REGION")
    endpoint = os.environ.get("SPEECH_ENDPOINT")
    resource_id = os.environ.get("SPEECH_RESOURCE_ID")
    if key and (region or endpoint):
        return True
    if endpoint:
        return True
    # Keyless: need a region to build the config and a resource id for the AAD
    # authorization token (``aad#<resource-id>#<token>``).
    if region and resource_id:
        return True
    return False


def voice_available() -> bool:
    return _voice_enabled() and _speech_configured()


def _is_hosted() -> bool:
    """Running inside a Foundry hosted agent (managed identity present)."""
    return bool(
        os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")
    )


# ── Keyless Speech token management ────────────────────────────────────────
_speech_credential = None
_speech_token_cache: dict[str, Any] = {"token": None, "expires_on": 0.0}


def _get_speech_credential():
    global _speech_credential
    if _speech_credential is None:
        from azure.identity import DefaultAzureCredential

        # In a hosted agent we want the managed identity; locally we want the
        # developer's `az login` / VS Code / env credential and must exclude the
        # (absent) managed identity probe so it fails fast rather than hanging.
        _speech_credential = DefaultAzureCredential(
            exclude_managed_identity_credential=not _is_hosted()
        )
    return _speech_credential


def get_speech_token(force: bool = False) -> Optional[str]:
    """Return a cached AAD bearer token for the Speech resource (keyless auth)."""
    if os.environ.get("SPEECH_KEY"):
        return None  # key-based auth, no token needed
    now = time.time()
    if (
        not force
        and _speech_token_cache["token"]
        and _speech_token_cache["expires_on"] - now > 300
    ):
        return _speech_token_cache["token"]
    try:
        cred = _get_speech_credential()
        tok = cred.get_token(SPEECH_AAD_SCOPE)
        _speech_token_cache["token"] = tok.token
        _speech_token_cache["expires_on"] = float(tok.expires_on)
        return tok.token
    except Exception:
        logger.warning("Failed to acquire Speech AAD token.", exc_info=True)
        return None


def prewarm_speech_auth() -> None:
    """Acquire a token early so the first turn is not delayed by auth latency."""
    try:
        get_speech_token(force=True)
    except Exception:
        logger.debug("Speech auth prewarm failed (non-fatal).", exc_info=True)


def _build_speech_config():
    """Build a SpeechConfig from key, endpoint, or region + AAD token."""
    import azure.cognitiveservices.speech as speechsdk

    key = os.environ.get("SPEECH_KEY")
    region = os.environ.get("SPEECH_REGION")
    endpoint = os.environ.get("SPEECH_ENDPOINT")
    resource_id = os.environ.get("SPEECH_RESOURCE_ID")

    if key and region:
        cfg = speechsdk.SpeechConfig(subscription=key, region=region)
    elif key and endpoint:
        cfg = speechsdk.SpeechConfig(subscription=key, endpoint=endpoint)
    else:
        # Keyless: region + AAD authorization token for the Speech resource.
        token = get_speech_token()
        if not token or not resource_id:
            raise RuntimeError(
                "Keyless Speech auth requires SPEECH_REGION, SPEECH_RESOURCE_ID "
                "and a valid AAD token."
            )
        auth = f"aad#{resource_id}#{token}"
        if region:
            cfg = speechsdk.SpeechConfig(auth_token=auth, region=region)
        elif endpoint:
            cfg = speechsdk.SpeechConfig(auth_token=auth, endpoint=endpoint)
        else:
            raise RuntimeError("Keyless Speech auth requires SPEECH_REGION.")

    cfg.speech_recognition_language = SPEECH_RECOGNITION_LANGUAGE
    cfg.speech_synthesis_voice_name = SPEECH_VOICE_NAME
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
    )
    # Shorten the trailing-silence window STT waits for before emitting a final
    # result. The default (~500 ms+) makes brief push-to-talk utterances feel
    # like they need an extra beat of holding before release; 300 ms lets short
    # phrases finalize promptly without truncating normal speech.
    cfg.set_property(
        speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, "300"
    )
    return cfg


# ── Text → speakable prose ─────────────────────────────────────────────────
# Defensive: strip fenced code blocks (agent replies rarely contain them, but
# never read code aloud if one slips through).
_FENCE_RE = re.compile(r"```[a-z]*\s*\n.*?```", re.IGNORECASE | re.DOTALL)
# Markdown image: ![alt](url) — Blender screenshots / renders are embedded this
# way. Never spoken (the URL and alt text are visual, not conversational).
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Markdown link: [label](url) — download buttons. Keep the label, drop the URL.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_TICK_RE = re.compile(r"`([^`]+)`")
_MD_EMPH_RE = re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}")


def strip_media(text: str) -> str:
    """Remove fenced code blocks and markdown images from *text*."""
    if not text:
        return ""
    out = _FENCE_RE.sub(" ", text)
    out = _MD_IMAGE_RE.sub(" ", out)
    return out


def _speakable_prose(text: str) -> str:
    """Return only the prose safe to speak *right now* from a streaming buffer.

    Removes complete fenced blocks and images, and drops any trailing token
    that is still being streamed (an unclosed fence or an unclosed image/link)
    so we never read a partial ``![screensh`` or a raw URL aloud.
    """
    if not text:
        return ""
    prose = _FENCE_RE.sub(" ", text)
    prose = _MD_IMAGE_RE.sub(" ", prose)
    # Drop an unclosed trailing fence.
    open_fence = prose.rfind("```")
    if open_fence != -1:
        prose = prose[:open_fence]
    # Drop an unclosed trailing image/link ("![" or "[" with no closing ")").
    cut = max(prose.rfind("!["), prose.rfind("["))
    if cut != -1 and prose.find(")", cut) == -1:
        prose = prose[:cut]
    return prose


def _normalize_for_speech(text: str) -> str:
    """Turn a chunk of markdown prose into a clean utterance for TTS."""
    if not text:
        return ""
    out = _MD_IMAGE_RE.sub(" ", text)  # defensive: never read image markdown
    out = _MD_LINK_RE.sub(r"\1", out)  # [label](url) -> label
    out = _MD_TICK_RE.sub(r"\1", out)  # `code` -> code
    out = _MD_EMPH_RE.sub(r"\1", out)  # *bold* / _em_ -> plain
    out = re.sub(r"^\s{0,3}#{1,6}\s*", "", out, flags=re.MULTILINE)  # headings
    out = re.sub(r"^\s*[-*+]\s+", "", out, flags=re.MULTILINE)  # bullets
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def _reply_has_image(reply: str) -> bool:
    return bool(reply and _MD_IMAGE_RE.search(reply))


def _reply_has_download(reply: str) -> bool:
    """True when the reply contains a markdown link that is not an image."""
    if not reply:
        return False
    without_images = _MD_IMAGE_RE.sub(" ", reply)
    return bool(_MD_LINK_RE.search(without_images))


# ── Progress narration phrase pools ────────────────────────────────────────
PROGRESS_OPENERS = (
    "Okay, let me work on that.",
    "Sure — give me a moment.",
    "On it. Setting that up now.",
    "Got it. Working on the scene.",
    "Alright, let me put that together.",
)
PROGRESS_WORKING_KW = (
    "Still working on the {kw}…",
    "Adding the {kw} to the scene…",
    "Almost there with the {kw}…",
    "Just finishing the {kw}…",
)
PROGRESS_WORKING_GENERIC = (
    "Still working on it…",
    "Almost there…",
    "Just a moment more…",
    "Putting it together…",
)
PROGRESS_CLOSERS_KW = (
    "There you go — the {kw} is in the scene now.",
    "All set. Take a look at the {kw}.",
    "Done! Your {kw} is ready in the viewport.",
    "And there's the {kw}.",
)
PROGRESS_CLOSERS_GENERIC = (
    "There you go — take a look at the scene.",
    "All done. Here's your scene.",
    "Done — the viewport is updated.",
    "And there it is.",
)

_KW_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "with", "for",
    "please", "can", "you", "could", "would", "make", "create", "add",
    "give", "me", "some", "that", "this", "it", "scene", "blender",
    "render", "screenshot", "show", "put", "set", "up",
}


def _extract_keyword(transcript: str) -> Optional[str]:
    """Pick a salient noun-ish word from the user's request for narration."""
    if not transcript:
        return None
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", transcript.lower())
    for w in words:
        if w not in _KW_STOPWORDS:
            return w
    return None


# ── Streaming sentence segmentation ────────────────────────────────────────
# A "sentence" ends at .!?: followed by whitespace/quote/close-bracket/EOL, or a
# newline. Non-greedy so we emit as soon as a boundary is seen.
_SENTENCE_RE = re.compile(r".*?(?:[.!?:](?=[\s\"')\]]|$)|\n)", re.DOTALL)


class _ProseSentenceStreamer:
    """Feed streaming deltas, yield complete speakable sentences as they form.

    We keep the full raw buffer (so partial markdown tokens can complete across
    deltas) and track how much of the *speakable* projection has already been
    emitted. Fenced code blocks and markdown images are stripped incrementally
    via :func:`_speakable_prose`, so they are never queued for speech.
    """

    def __init__(self) -> None:
        self._buf = ""       # raw accumulated text
        self._emitted = 0    # speakable chars already emitted

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._buf += delta
        return self._drain(final=False)

    def flush(self) -> list[str]:
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[str]:
        speakable = _speakable_prose(self._buf)
        out: list[str] = []
        pos = min(self._emitted, len(speakable))
        for m in _SENTENCE_RE.finditer(speakable, pos):
            norm = _normalize_for_speech(m.group(0))
            if norm:
                out.append(norm)
            pos = m.end()
        self._emitted = pos
        if final:
            tail = _normalize_for_speech(speakable[pos:])
            if tail:
                out.append(tail)
            self._buf = ""
            self._emitted = 0
        return out


# ── Voice session ──────────────────────────────────────────────────────────
class VoiceSession:
    """One browser voice connection: STT capture → agent turn → TTS stream."""

    def __init__(
        self,
        agent: Any,
        send_text: Callable[[dict], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        session_id: Optional[str] = None,
        call_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._agent = agent
        self._send_text_cb = send_text
        self._send_bytes_cb = send_bytes

        # Continuity / scene key (owned by the browser, arrives on control frames).
        self._conversation_id: Optional[str] = session_id
        self._previous_response_id: Optional[str] = None
        # Foundry routing and Responses conversation ids injected by the relay.
        # The session supplies sandbox affinity; the conversation supplies the
        # persisted transcript shared by typed and voice turns.
        self._agent_session_id: Optional[str] = None
        self._foundry_conversation_id: Optional[str] = None
        # Degradation only: if the web relay cannot resolve a Foundry
        # conversation, retain voice-to-voice context for this socket. Normal
        # hosted turns use `conversation`; local turns use previous_response_id.
        self._fallback_history: list[dict[str, str]] = []
        self._history_max_messages = 8
        # Platform per-request call id (``x-agent-foundry-call-id``) captured from
        # the inbound WS upgrade. The hosted responses protocol v2.0.0 REQUIRES
        # it on every /responses call, so we forward it on the in-container
        # loopback call (otherwise the handler fails fast with "the hosted
        # environment is running on protocol 1.0.0").
        self._call_id: Optional[str] = call_id
        self._user_id: Optional[str] = user_id

        # STT state.
        self._recognizer = None
        self._push_stream = None
        self._recognized_parts: list[str] = []
        self._capturing = False
        self._stt_started_at: Optional[float] = None
        self._stt_commit_at: Optional[float] = None
        self._stt_audio_bytes = 0
        self._stt_error_code: Optional[str] = None
        self._stt_span = None

        # TTS worker state.
        self._tts_queue: "asyncio.Queue[Optional[tuple[str, Any]]]" = asyncio.Queue()
        self._tts_task: Optional[asyncio.Task] = None
        self._speaking = False

        # Turn / barge-in state.
        self._turn_task: Optional[asyncio.Task] = None
        self._cancelled = False
        self._turn_ready_at: Optional[float] = None
        self._first_audio_sent = False

        # STT finalization signalling. Continuous recognition delivers the FINAL
        # `recognized` result asynchronously (after it drains trailing audio and
        # detects end-of-utterance), so `_commit_and_run` must wait for it rather
        # than read immediately (else the transcript is empty → "Didn't catch
        # that"). Set from the SDK's background thread via the captured loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stt_done: Optional[asyncio.Event] = None

    # -- outbound helpers ---------------------------------------------------
    async def _send_text(self, obj: dict) -> None:
        try:
            await self._send_text_cb(obj)
            size = len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
            attrs = {"frame.type": "text"}
            _count(_frames_sent, 1, attrs)
            _count(_bytes_sent, size, attrs)
        except Exception:
            logger.debug("send_text failed", exc_info=True)

    async def _send_bytes(self, data: bytes) -> None:
        try:
            await self._send_bytes_cb(data)
            attrs = {"frame.type": "binary"}
            _count(_frames_sent, 1, attrs)
            _count(_bytes_sent, len(data), attrs)
        except Exception:
            logger.debug("send_bytes failed", exc_info=True)

    def record_received_frame(self, message: Any) -> None:
        is_binary = isinstance(message, (bytes, bytearray))
        size = len(message) if is_binary else len(str(message).encode("utf-8"))
        attrs = {"frame.type": "binary" if is_binary else "text"}
        _count(_frames_received, 1, attrs)
        _count(_bytes_received, size, attrs)

    # -- control frames -----------------------------------------------------
    async def on_control(self, message: dict) -> None:
        mtype = (message or {}).get("type")
        cid = (message or {}).get("conversation_id")
        if isinstance(cid, str) and cid:
            self._conversation_id = cid
        prev = (message or {}).get("previous_response_id")
        if isinstance(prev, str) and prev:
            self._previous_response_id = prev
        # Real Foundry agent_session_id injected by the relay (foundry mode) so
        # voice threads into the SAME server session as text.
        fsid = (message or {}).get("foundry_agent_session_id")
        if isinstance(fsid, str) and fsid:
            self._agent_session_id = fsid
        fcid = (message or {}).get("foundry_conversation_id")
        if isinstance(fcid, str) and fcid:
            self._foundry_conversation_id = fcid

        if mtype == "start":
            await self._start_capture()
        elif mtype == "context":
            # continuity hint only — values already captured above
            pass
        elif mtype == "commit":
            await self._commit_and_run()
        elif mtype == "cancel":
            await self._barge_in()
        else:
            logger.debug("Ignoring unknown control frame: %r", mtype)

    async def on_audio(self, chunk: bytes) -> None:
        if not self._capturing or not self._push_stream:
            return
        self._stt_audio_bytes += len(chunk)
        try:
            self._push_stream.write(chunk)
        except Exception:
            logger.debug("push_stream.write failed", exc_info=True)

    # -- STT capture --------------------------------------------------------
    async def _start_capture(self) -> None:
        if self._capturing:
            return
        # A new capture cancels any in-flight speech (barge-in).
        await self._barge_in()
        self._stt_started_at = time.perf_counter()
        self._stt_commit_at = None
        self._stt_audio_bytes = 0
        self._stt_error_code = None
        self._stt_span = (
            _tracer.start_span(
                "voice.stt",
                attributes={
                    "voice.stt.provider": "azure_speech",
                    "voice.stt.language": SPEECH_RECOGNITION_LANGUAGE,
                    "audio.sample_rate": SAMPLE_RATE,
                    "audio.channels": CHANNELS,
                },
            )
            if _tracer is not None
            else None
        )
        try:
            import azure.cognitiveservices.speech as speechsdk

            cfg = _build_speech_config()
            fmt = speechsdk.audio.AudioStreamFormat(
                samples_per_second=SAMPLE_RATE,
                bits_per_sample=BITS_PER_SAMPLE,
                channels=CHANNELS,
            )
            self._push_stream = speechsdk.audio.PushAudioInputStream(fmt)
            audio_config = speechsdk.audio.AudioConfig(stream=self._push_stream)
            self._recognizer = speechsdk.SpeechRecognizer(
                speech_config=cfg, audio_config=audio_config
            )
            self._recognized_parts = []
            self._loop = asyncio.get_running_loop()
            self._stt_done = asyncio.Event()

            def _signal_stt_done() -> None:
                loop = self._loop
                ev = self._stt_done
                if loop is not None and ev is not None:
                    loop.call_soon_threadsafe(ev.set)

            def _on_recognized(evt) -> None:
                try:
                    if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                        txt = (evt.result.text or "").strip()
                        if txt:
                            self._recognized_parts.append(txt)
                except Exception:
                    logger.debug("recognized handler failed", exc_info=True)

            def _on_canceled(evt) -> None:
                # Surface STT-side failures (auth/RBAC, network, quota) that are
                # otherwise silent (they just yield an empty transcript). Logs
                # only — the SDK fires this on a background thread where we can't
                # await an outbound frame.
                try:
                    self._stt_error_code = str(getattr(evt, "error_code", "canceled"))
                    logger.warning(
                        "STT canceled: reason=%s error_code=%s details=%s",
                        getattr(evt, "reason", "?"),
                        getattr(evt, "error_code", "?"),
                        getattr(evt, "error_details", ""),
                    )
                except Exception:
                    logger.debug("canceled handler failed", exc_info=True)
                _signal_stt_done()

            def _on_session_stopped(evt) -> None:
                # Fires after the recognizer drains the closed input stream and
                # finalizes — the point at which all `recognized` results are in.
                _signal_stt_done()

            self._recognizer.recognized.connect(_on_recognized)
            self._recognizer.canceled.connect(_on_canceled)
            self._recognizer.session_stopped.connect(_on_session_stopped)
            self._recognizer.start_continuous_recognition_async()
            self._capturing = True
            await self._send_text({"type": "listening"})
        except Exception as exc:
            logger.warning("Failed to start STT capture.", exc_info=True)
            self._finish_stt_span(exc=exc, error_code="start_failed")
            await self._send_text(
                {"type": "error", "message": "Could not start speech recognition."}
            )

    def _finish_stt_span(
        self,
        *,
        transcript: str = "",
        exc: BaseException | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._stt_started_at is None:
            return
        finished_at = time.perf_counter()
        duration_ms = (finished_at - self._stt_started_at) * 1000
        finalize_ms = (
            (finished_at - self._stt_commit_at) * 1000
            if self._stt_commit_at is not None
            else 0.0
        )
        audio_ms = self._stt_audio_bytes / (SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE // 8)) * 1000
        attributes = {
            "voice.stt.language": SPEECH_RECOGNITION_LANGUAGE,
            "voice.stt.empty": not bool(transcript),
        }
        _record(_stt_duration, duration_ms, attributes)
        if self._stt_commit_at is not None:
            _record(_stt_finalize_duration, finalize_ms, attributes)
        span = self._stt_span
        if span is not None:
            span.set_attribute("voice.stt.duration_ms", duration_ms)
            span.set_attribute("voice.stt.finalization.duration_ms", finalize_ms)
            span.set_attribute("voice.stt.input_audio.duration_ms", audio_ms)
            span.set_attribute("voice.stt.input_audio.bytes", self._stt_audio_bytes)
            span.set_attribute("voice.stt.transcript.characters", len(transcript))
            span.set_attribute("voice.stt.empty", not bool(transcript))
            resolved_error = error_code or self._stt_error_code
            if exc is not None or resolved_error:
                _set_span_error(span, exc, resolved_error)
            span.end()
        resolved_error = error_code or self._stt_error_code
        if exc is not None or resolved_error:
            _record_failure("stt", exc, resolved_error)
        self._stt_started_at = None
        self._stt_commit_at = None
        self._stt_span = None

    def _stop_capture(self) -> None:
        self._capturing = False
        rec = self._recognizer
        stream = self._push_stream
        self._recognizer = None
        self._push_stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if rec is not None:
            try:
                rec.stop_continuous_recognition_async()
            except Exception:
                pass
        self._finish_stt_span(error_code="capture_closed")

    async def _commit_and_run(self) -> None:
        if not self._capturing:
            # Nothing was being captured; ignore stray commit.
            return
        self._capturing = False
        self._stt_commit_at = time.perf_counter()
        # Signal end-of-audio and WAIT for the recognizer to finalize before
        # reading the transcript. Continuous recognition emits the final
        # `recognized` result only after it drains the trailing audio and
        # detects end-of-utterance, so a fixed short sleep races it (→ empty
        # transcript → "Didn't catch that"). Closing the push stream ends the
        # input; `session_stopped`/`canceled` then set `_stt_done`.
        rec = self._recognizer
        stream = self._push_stream
        self._recognizer = None
        self._push_stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if self._stt_done is not None:
            try:
                await asyncio.wait_for(self._stt_done.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                self._stt_error_code = "finalize_timeout"
                logger.info(
                    "STT finalize wait timed out (8s) — using whatever was recognized."
                )
        # Tiny grace so a final `recognized` immediately preceding
        # `session_stopped` is appended before we read.
        await asyncio.sleep(0.1)
        if rec is not None:
            try:
                rec.stop_continuous_recognition_async()
            except Exception:
                pass
        transcript = " ".join(p for p in self._recognized_parts if p).strip()
        self._recognized_parts = []
        self._finish_stt_span(transcript=transcript)
        logger.info("Voice commit: transcript=%r", transcript[:200])
        if not transcript:
            await self._send_text(
                {"type": "stt", "text": "", "final": True}
            )
            await self._send_text({"type": "done", "reply": "", "empty": True})
            return
        await self._send_text({"type": "stt", "text": transcript, "final": True})
        self._cancelled = False
        self._turn_ready_at = time.perf_counter()
        self._first_audio_sent = False
        self._turn_task = asyncio.ensure_future(self._run_turn(transcript))

    # -- barge-in / cancel --------------------------------------------------
    async def _barge_in(self) -> None:
        self._cancelled = True
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tts_task
        self._tts_task = None
        # Drain the pending TTS queue.
        try:
            while True:
                self._tts_queue.get_nowait()
                self._tts_queue.task_done()
        except asyncio.QueueEmpty:
            pass
        self._tts_queue = asyncio.Queue()
        if self._speaking:
            await self._send_text({"type": "speaking_end"})
            self._speaking = False

    # -- the agent turn -----------------------------------------------------
    async def _run_turn(self, transcript: str) -> None:
        started_at = time.perf_counter()
        attributes = {
            "voice.turn.mode": "hosted" if _is_hosted() else "local",
            "voice.turn.input.characters": len(transcript),
        }
        with _span("voice.turn", attributes) as span:
            try:
                await self._run_turn_inner(transcript)
            except asyncio.CancelledError:
                if span is not None:
                    span.set_attribute("voice.turn.cancelled", True)
                raise
            except Exception as exc:
                _record_failure("turn", exc)
                raise
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000
                _record(_turn_duration, duration_ms, {"voice.turn.mode": attributes["voice.turn.mode"]})
                if span is not None:
                    span.set_attribute("voice.turn.duration_ms", duration_ms)

    async def _run_turn_inner(self, transcript: str) -> None:
        keyword = _extract_keyword(transcript)
        state = {"prose": 0, "kw": keyword}
        self._ensure_tts_worker()
        progress_task = asyncio.ensure_future(self._progress_loop(state))
        reply = ""
        errored = False
        try:
            reply = await self._stream_agent(transcript, state)
        except asyncio.CancelledError:
            progress_task.cancel()
            raise
        except Exception as exc:
            errored = True
            logger.warning("Voice turn failed.", exc_info=True)
            await self._send_text({"type": "error", "message": str(exc)})
        finally:
            progress_task.cancel()

        if self._cancelled:
            return

        # Only the hosted degradation path needs inline replay. Normally the
        # shared Foundry conversation (hosted) or response chain (local) owns
        # history and is also visible to typed turns.
        if _is_hosted() and not self._foundry_conversation_id:
            self._fallback_history.append({"role": "user", "content": transcript})
            if reply:
                self._fallback_history.append({"role": "assistant", "content": reply})
            if len(self._fallback_history) > self._history_max_messages:
                self._fallback_history = self._fallback_history[-self._history_max_messages:]

        if errored:
            return

        has_image = _reply_has_image(reply)
        has_download = _reply_has_download(reply)

        # If nothing prose-like was spoken and there is no visual/download
        # result, speak the stripped reply so the user always hears something.
        if state["prose"] == 0 and not has_image and not has_download:
            fallback = _normalize_for_speech(strip_media(reply)).strip()
            if fallback:
                await self._queue_tts(fallback)

        # Always announce a visual result or a download (user cue requirement).
        if has_image:
            kw = state.get("kw")
            cue = (
                random.choice(PROGRESS_CLOSERS_KW).format(kw=kw)
                if kw
                else random.choice(PROGRESS_CLOSERS_GENERIC)
            )
            await self._send_text({"type": "progress", "text": cue})
            await self._queue_tts(cue)
        elif has_download:
            cue = "Your download link is ready below."
            await self._send_text({"type": "progress", "text": cue})
            await self._queue_tts(cue)

        # The server's `done` event means all response audio has been emitted,
        # not merely queued. This keeps status, metrics, and barge-in state in
        # sync with the actual wire behavior.
        await self._tts_queue.join()
        if self._tts_task and not self._tts_task.done():
            await self._tts_queue.put(None)
            await self._tts_task
        self._tts_task = None
        if self._speaking:
            await self._send_text({"type": "speaking_end"})
            self._speaking = False

        # Local mode can safely advance the same response chain used by typed
        # chat. Hosted mode uses the shared conversation instead; its in-process
        # response id is not a valid public-gateway continuation id.
        await self._send_text(
            {
                "type": "done",
                "reply": reply,
                "response_id": None if _is_hosted() else self._previous_response_id,
            }
        )

    async def _stream_agent(self, transcript: str, state: dict) -> str:
        started_at = time.perf_counter()
        state["agent_started_at"] = started_at
        state["agent_first_delta_recorded"] = False
        attributes = {
            "voice.agent.protocol": "responses",
            "voice.agent.history_mode": (
                "conversation"
                if self._foundry_conversation_id
                else "previous_response"
                if self._previous_response_id
                else "inline_fallback"
                if self._fallback_history
                else "new"
            ),
        }
        with _span("voice.agent", attributes) as span:
            try:
                reply = await self._stream_agent_inner(transcript, state)
                if span is not None:
                    span.set_attribute("voice.agent.output.characters", len(reply))
                return reply
            except Exception as exc:
                _record_failure("agent", exc)
                raise
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000
                _record(_agent_duration, duration_ms, attributes)
                if span is not None:
                    span.set_attribute("voice.agent.duration_ms", duration_ms)

    def _build_agent_request(self, transcript: str) -> dict[str, Any]:
        """Build the shared-conversation Responses payload for one voice turn."""
        input_value: Any = transcript
        if _is_hosted() and not self._foundry_conversation_id and self._fallback_history:
            input_value = [
                *self._fallback_history,
                {"role": "user", "content": transcript},
            ]
        body: dict[str, Any] = {
            "model": AGENT_MODEL,
            "input": input_value,
            "stream": True,
        }
        if self._foundry_conversation_id:
            body["conversation"] = self._foundry_conversation_id
        elif self._previous_response_id:
            body["previous_response_id"] = self._previous_response_id
        if self._agent_session_id:
            body["agent_session_id"] = self._agent_session_id
        elif not _is_hosted() and self._conversation_id:
            body["agent_session_id"] = self._conversation_id
        if self._conversation_id:
            body["user"] = self._conversation_id
            body["metadata"] = {"conversation_id": self._conversation_id}
        return body

    def _build_loopback_headers(self) -> dict[str, str]:
        """Inject trace and hosted identity context into the local HTTP hop."""
        headers = {"Accept": "text/event-stream"}
        try:
            from opentelemetry.propagate import inject

            inject(headers)
        except ImportError:
            pass
        if self._call_id:
            headers[FOUNDRY_CALL_ID_HEADER] = self._call_id
        if self._user_id:
            headers[FOUNDRY_USER_ID_HEADER] = self._user_id
        return headers

    async def _stream_agent_inner(self, transcript: str, state: dict) -> str:
        """POST to the local /responses endpoint and stream the reply back."""
        import httpx

        streamer = _ProseSentenceStreamer()
        reply_parts: list[str] = []

        # Hosted mode passes the same Foundry Responses conversation used by
        # typed chat. Local mode chains previous_response_id. If hosted
        # conversation resolution failed, combine the last public typed chain
        # (when available) with a small inline voice-only fallback.
        body = self._build_agent_request(transcript)

        timeout = httpx.Timeout(600.0, connect=10.0)
        headers = self._build_loopback_headers()
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                LOCAL_RESPONSES_URL,
                json=body,
                headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"/responses {resp.status_code}: {detail[:400]}")
                event = "message"
                data_lines: list[str] = []
                async for raw_line in resp.aiter_lines():
                    if self._cancelled:
                        break
                    line = raw_line.rstrip("\r")
                    if line == "":
                        if data_lines:
                            await self._handle_sse(
                                event, "\n".join(data_lines), streamer, reply_parts, state
                            )
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].lstrip())
                if data_lines and not self._cancelled:
                    await self._handle_sse(
                        event, "\n".join(data_lines), streamer, reply_parts, state
                    )

        # Flush any trailing prose to speech.
        for sentence in streamer.flush():
            state["prose"] += 1
            await self._queue_tts(sentence)

        return "".join(reply_parts)

    async def _handle_sse(
        self,
        event: str,
        data: str,
        streamer: _ProseSentenceStreamer,
        reply_parts: list[str],
        state: dict,
    ) -> None:
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        etype = event if event and event != "message" else payload.get("type")

        if etype == "response.created":
            rid = (payload.get("response") or {}).get("id")
            if rid:
                self._previous_response_id = rid
        elif etype == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if not state.get("agent_first_delta_recorded"):
                    state["agent_first_delta_recorded"] = True
                    latency_ms = (
                        time.perf_counter() - state.get("agent_started_at", time.perf_counter())
                    ) * 1000
                    attrs = {"voice.agent.protocol": "responses"}
                    _record(_agent_first_delta, latency_ms, attrs)
                    if otel_trace is not None:
                        current_span = otel_trace.get_current_span()
                        current_span.set_attribute(
                            "voice.agent.time_to_first_delta_ms", latency_ms
                        )
                reply_parts.append(delta)
                # Forward the raw delta so the UI extracts *status* pills exactly
                # like the typed path.
                await self._send_text({"type": "delta", "text": delta})
                for sentence in streamer.feed(delta):
                    state["prose"] += 1
                    await self._queue_tts(sentence)
        elif etype in ("response.completed", "response.incomplete"):
            rid = (payload.get("response") or {}).get("id")
            if rid:
                self._previous_response_id = rid
        elif etype in ("response.failed", "error"):
            # Dig out the most specific message the platform gives us.
            err = payload.get("error")
            if not isinstance(err, dict):
                err = (payload.get("response") or {}).get("error") or {}
            msg = (
                (err.get("message") if isinstance(err, dict) else None)
                or payload.get("message")
                or json.dumps(payload)[:500]
            )
            # A FAILED PERSISTENCE to Foundry storage is NON-FATAL for us: the
            # reply content has already streamed back in full (deltas above) and
            # we keep our own inline history, so retrieval-side storage is
            # irrelevant to the voice turn. The hosted `/storage/responses`
            # backend is intermittently returning 500 and surfaces it as a
            # terminal `error` frame — swallow it (log only) so the turn still
            # completes with speech + a proper `done`, instead of failing a fully
            # generated answer. We keep store=True (default) so the Foundry
            # portal still gets response traces when persistence succeeds.
            low = (msg or "").lower()
            if (
                "storing the response" in low
                or "while storing" in low
                or "retrieval is not guaranteed" in low
            ):
                logger.warning(
                    "Voice: ignoring non-fatal storage persistence error: %s", msg
                )
                return
            # Anything else (model/tool/agent failure) IS fatal — abort the turn
            # so the error reaches the browser and the container log.
            logger.warning("Voice: /responses emitted an error event: %s", msg)
            raise RuntimeError(msg)

    # -- progress narration -------------------------------------------------
    async def _progress_loop(self, state: dict) -> None:
        """Fill silent gaps (while tools run) with light narration."""
        try:
            await asyncio.sleep(PROGRESS_FIRST_MS / 1000.0)
            if self._cancelled:
                return
            if state["prose"] == 0 and self._tts_queue.empty() and not self._speaking:
                opener = random.choice(PROGRESS_OPENERS)
                await self._send_text({"type": "progress", "text": opener})
                await self._queue_tts(opener)
            while not self._cancelled:
                await asyncio.sleep(PROGRESS_INTERVAL_MS / 1000.0)
                if self._cancelled:
                    return
                if self._tts_queue.empty() and not self._speaking:
                    kw = state.get("kw")
                    line = (
                        random.choice(PROGRESS_WORKING_KW).format(kw=kw)
                        if kw
                        else random.choice(PROGRESS_WORKING_GENERIC)
                    )
                    await self._send_text({"type": "progress", "text": line})
                    await self._queue_tts(line)
        except asyncio.CancelledError:
            pass

    # -- TTS worker ---------------------------------------------------------
    def _ensure_tts_worker(self) -> None:
        if self._tts_task is None or self._tts_task.done():
            self._tts_task = asyncio.ensure_future(self._tts_worker_loop())

    async def _queue_tts(self, sentence: str) -> None:
        parent_context = otel_context.get_current() if otel_context is not None else None
        await self._tts_queue.put((sentence, parent_context))

    async def _tts_worker_loop(self) -> None:
        while True:
            item = await self._tts_queue.get()
            try:
                if item is None:
                    return
                sentence, parent_context = item
                if self._cancelled or not sentence.strip():
                    continue
                token = (
                    otel_context.attach(parent_context)
                    if otel_context is not None and parent_context is not None
                    else None
                )
                try:
                    await self._synthesize_and_stream(sentence)
                finally:
                    if otel_context is not None and token is not None:
                        otel_context.detach(token)
            finally:
                self._tts_queue.task_done()

    def _make_synthesizer(self, voice_name: str):
        import azure.cognitiveservices.speech as speechsdk

        cfg = _build_speech_config()
        cfg.speech_synthesis_voice_name = voice_name
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
        )
        # audio_config=None → keep the synthesized audio in memory (do not play
        # to a local speaker) so we can forward the raw PCM to the browser.
        return speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)

    async def _synthesize_and_stream(self, sentence: str) -> None:
        import azure.cognitiveservices.speech as speechsdk

        started_at = time.perf_counter()
        attributes = {"voice.tts.provider": "azure_speech"}
        if not self._speaking:
            self._speaking = True
            await self._send_text({"type": "speaking_start"})

        async def _run(voice_name: str) -> tuple[bool, int]:
            synth = self._make_synthesizer(voice_name)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: synth.speak_text_async(sentence).get()
            )
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Raw24Khz16BitMonoPcm → header-less PCM. Forward in 20 ms frames.
                data = result.audio_data or b""
                for i in range(0, len(data), TTS_FRAME_BYTES):
                    if self._cancelled:
                        return True, len(data)
                    if not self._first_audio_sent and self._turn_ready_at is not None:
                        self._first_audio_sent = True
                        first_audio_ms = (time.perf_counter() - self._turn_ready_at) * 1000
                        _record(_turn_first_audio, first_audio_ms)
                        if otel_trace is not None:
                            otel_trace.get_current_span().set_attribute(
                                "voice.turn.time_to_first_audio_ms", first_audio_ms
                            )
                    await self._send_bytes(data[i : i + TTS_FRAME_BYTES])
                return True, len(data)
            if result.reason == speechsdk.ResultReason.Canceled:
                details = result.cancellation_details
                logger.warning(
                    "TTS canceled (voice=%s): %s / %s",
                    voice_name,
                    details.reason,
                    details.error_details,
                )
                return False, 0
            return False, 0

        with _span("voice.tts", attributes) as span:
            try:
                voice_name = SPEECH_VOICE_NAME
                ok, audio_bytes = await _run(voice_name)
                used_fallback = False
                if not ok and SPEECH_VOICE_FALLBACK and SPEECH_VOICE_FALLBACK != SPEECH_VOICE_NAME:
                    used_fallback = True
                    voice_name = SPEECH_VOICE_FALLBACK
                    logger.info("Retrying TTS with fallback voice %s", voice_name)
                    ok, audio_bytes = await _run(voice_name)
                audio_ms = audio_bytes / (SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE // 8)) * 1000
                if span is not None:
                    span.set_attribute("voice.tts.voice", voice_name)
                    span.set_attribute("voice.tts.fallback", used_fallback)
                    span.set_attribute("voice.tts.input.characters", len(sentence))
                    span.set_attribute("voice.tts.output_audio.bytes", audio_bytes)
                    span.set_attribute("voice.tts.output_audio.duration_ms", audio_ms)
                if not ok:
                    _record_failure("tts", code="synthesis_canceled")
                    _set_span_error(span, code="synthesis_canceled")
            except Exception as exc:
                logger.warning("TTS synthesis failed.", exc_info=True)
                _record_failure("tts", exc)
                _set_span_error(span, exc)
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000
                _record(_tts_duration, duration_ms, attributes)
                if span is not None:
                    span.set_attribute("voice.tts.duration_ms", duration_ms)

    # -- lifecycle ----------------------------------------------------------
    async def close(self) -> None:
        self._cancelled = True
        self._stop_capture()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tts_task
        if self._speaking:
            self._speaking = False


# ── Transport-agnostic driver ──────────────────────────────────────────────
async def drive_connection(
    agent: Any,
    *,
    send_text: Callable[[dict], Awaitable[None]],
    send_bytes: Callable[[bytes], Awaitable[None]],
    incoming: AsyncIterator[Any],
    session_id: Optional[str] = None,
    call_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Drive one voice connection given an async iterator of inbound messages.

    ``incoming`` yields ``str`` (JSON control frames) or ``bytes`` (PCM audio).
    """
    session = VoiceSession(
        agent,
        send_text,
        send_bytes,
        session_id=session_id,
        call_id=call_id,
        user_id=user_id,
    )
    try:
        async for message in incoming:
            session.record_received_frame(message)
            if isinstance(message, (bytes, bytearray)):
                await session.on_audio(bytes(message))
            elif isinstance(message, str):
                try:
                    obj = json.loads(message)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON text frame.")
                    continue
                if isinstance(obj, dict):
                    await session.on_control(obj)
    finally:
        await session.close()


# ── WebSocket server (invocations_ws protocol) ─────────────────────────────
async def run_ws_server(agent: Any, *, host: str = "0.0.0.0", port: Optional[int] = None) -> None:
    """Serve the voice WebSocket forever on ``VOICE_WS_PATH``."""
    import websockets

    listen_port = port or VOICE_WS_PORT
    prewarm_speech_auth()

    async def handler(websocket) -> None:
        # Support both the `path` (older API) and `websocket.request.path`.
        path = getattr(websocket, "path", None)
        if path is None:
            request = getattr(websocket, "request", None)
            path = getattr(request, "path", VOICE_WS_PATH) if request else VOICE_WS_PATH
        if VOICE_WS_PATH not in (path or ""):
            logger.debug("Rejecting voice WS path: %r", path)
            await websocket.close(code=1008, reason="unexpected path")
            return

        session_id = None
        try:
            from urllib.parse import urlparse, parse_qs

            q = parse_qs(urlparse(path or "").query)
            sid = q.get("agent_session_id") or q.get("sessionId")
            if sid:
                session_id = sid[0]
        except Exception:
            pass

        # Platform per-request call id (present on hosted protocol v2.0.0
        # upgrades; absent in local dev). Forwarded to the loopback /responses.
        call_id = None
        user_id = None
        try:
            request = getattr(websocket, "request", None)
            hdrs = getattr(request, "headers", None)
            if hdrs is not None:
                call_id = hdrs.get(FOUNDRY_CALL_ID_HEADER)
                user_id = hdrs.get(FOUNDRY_USER_ID_HEADER)
        except Exception:
            pass

        async def send_text(obj: dict) -> None:
            await websocket.send(json.dumps(obj))

        async def send_bytes(data: bytes) -> None:
            await websocket.send(data)

        async def incoming() -> AsyncIterator[Any]:
            async for message in websocket:
                yield message

        logger.info(
            "Voice WS connection opened (session_id=%s, foundry_call_id=%s).",
            session_id, "present" if call_id else "ABSENT",
        )
        try:
            await drive_connection(
                agent,
                send_text=send_text,
                send_bytes=send_bytes,
                incoming=incoming(),
                session_id=session_id,
                call_id=call_id,
                user_id=user_id,
            )
        finally:
            logger.info("Voice WS connection closed.")

    async with websockets.serve(
        handler, host, listen_port, max_size=2 * 1024 * 1024
    ):
        logger.info(
            "Voice WebSocket listening on ws://%s:%d%s", host, listen_port, VOICE_WS_PATH
        )
        await asyncio.Future()  # run forever


def register_invocations_ws_route(agent: Any, host_server: Any) -> bool:
    """Register the voice handler through the Agent Server invocations SDK.

    The Foundry hosted-agent platform proxies **every** declared protocol
    (``responses`` *and* ``invocations_ws``) to the single agentserver port
    that :class:`ResponsesHostServer` listens on (see the official
    ``invocations_ws`` samples, which all serve the WebSocket on that same
    port via ``@app.ws_handler``). The standalone :func:`run_ws_server` on a
    separate port (8089) is therefore only reachable in local Docker, not
    through the Foundry gateway (which forwards the upgrade to the agentserver
    port and returns 403 when no ``/invocations_ws`` route exists there).

    ``host_server`` composes :class:`InvocationAgentServerHost` with the
    Responses and optional Activity hosts. Registering with ``ws_handler`` lets
    the SDK own accept/close handling, keep-alive, the connection-scoped OTel
    span, and structured lifecycle metrics. Returns ``True`` when registered.
    """
    if not voice_available():
        return False

    from starlette.websockets import WebSocket

    prewarm_speech_auth()

    async def endpoint(websocket: WebSocket) -> None:
        session_id = websocket.query_params.get(
            "agent_session_id"
        ) or websocket.query_params.get("sessionId")
        # Platform per-request call id injected on the upgrade (protocol v2.0.0).
        # Forwarded to the loopback /responses call so the hosted responses
        # handler accepts it. Logged present/ABSENT so we can tell whether the
        # platform injects it on invocations_ws upgrades.
        call_id = websocket.headers.get(FOUNDRY_CALL_ID_HEADER)
        user_id = websocket.headers.get(FOUNDRY_USER_ID_HEADER)

        async def send_text(obj: dict) -> None:
            await websocket.send_text(json.dumps(obj))

        async def send_bytes(data: bytes) -> None:
            await websocket.send_bytes(data)

        async def incoming() -> AsyncIterator[Any]:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is not None:
                    yield text
                    continue
                data = message.get("bytes")
                if data is not None:
                    yield data

        logger.info(
            "Voice WS connection opened on agentserver port (session_id=%s, foundry_call_id=%s).",
            session_id, "present" if call_id else "ABSENT",
        )
        try:
            await drive_connection(
                agent,
                send_text=send_text,
                send_bytes=send_bytes,
                incoming=incoming(),
                session_id=session_id,
                call_id=call_id,
                user_id=user_id,
            )
        finally:
            logger.info("Voice WS connection closed (agentserver port).")

    ws_handler = getattr(host_server, "ws_handler", None)
    if not callable(ws_handler):
        logger.warning(
            "Cannot register voice route: host server has no ws_handler decorator."
        )
        return False

    ws_handler(endpoint)
    logger.info("Voice route registered through ws_handler at %s.", VOICE_WS_PATH)
    return True
