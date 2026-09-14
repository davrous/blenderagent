"""Opt-in, independent token probes. Never used to authorize application calls."""

import asyncio
import base64
import contextvars
import hashlib
import json
import logging
import os
import platform
import re
import time
import uuid
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit


logger = logging.getLogger("blender_agent.auth_diagnostics")
_probing = contextvars.ContextVar("auth_diagnostic_probe", default=False)
_GUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
TOKEN_TIMEOUT_SECONDS = 10
CLOSE_TIMEOUT_SECONDS = 2
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"


class _ProbeLogFilter(logging.Filter):
    def filter(self, record):
        return not _probing.get() or record.name == logger.name


def _emit(run_id, event, **fields):
    logger.info("AUTH_DIAG %s", json.dumps(
        {"run_id": run_id, "event": event, **fields}, sort_keys=True,
    ))


def _origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        return None


def _token_claims(token):
    """Decode only allowlisted identifiers; these are NOT verified claims."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(parts[1]) > 65536:
            return {"format": "opaque"}
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(payload, dict):
            return {"format": "opaque"}
        claims = {"format": "jwt", "signature_verified": False}
        for name in ("oid", "tid", "appid", "azp"):
            value = payload.get(name)
            if isinstance(value, str) and re.fullmatch(_GUID, value):
                claims[name] = value
        for name in ("aud", "iss"):
            value = payload.get(name)
            if isinstance(value, str):
                claims[name] = value if re.fullmatch(_GUID, value) else _origin(value)
        for name in ("iat", "exp"):
            if type(payload.get(name)) is int:
                claims[name] = payload[name]
        if payload.get("idtyp") in ("app", "user"):
            claims["idtyp"] = payload["idtyp"]
        return claims
    except (ValueError, TypeError):
        return {"format": "opaque"}


def _error_details(error):
    text = str(error)
    details = {
        "error_type": type(error).__name__,
        "aadsts_codes": sorted(set(re.findall(r"\bAADSTS\d{5,8}\b", text))),
        "oauth_errors": [code for code in ("invalid_scope", "invalid_grant", "bad_request") if code in text],
    }
    for name, label in (("correlation_ids", "Correlation ID"), ("trace_ids", "Trace ID")):
        details[name] = sorted(set(re.findall(label + r":\s*(" + _GUID + ")", text, re.I)))
    details["policy_ids"] = sorted({
        identifier
        for section in re.findall(r"capolids.{0,250}", text)
        for identifier in re.findall(_GUID, section)
    })
    return details


def _snapshot():
    root = Path(__file__).resolve().parent
    packages = {}
    for name in (
        "azure-identity", "azure-core", "msal", "aiohttp", "httpx",
        "azure-ai-projects", "azure-storage-blob", "openai", "microsoft-opentelemetry",
        "azure-ai-agentserver-core", "azure-ai-agentserver-responses",
        "azure-ai-agentserver-invocations", "azure-ai-agentserver-activity",
        "agent-framework-core", "agent-framework-foundry",
        "agent-framework-foundry-hosting", "azure-cognitiveservices-speech",
    ):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = "not-installed"
    hashes = {}
    for name in (
        "main.py", "auth_diagnostics.py", "voice_pipeline.py", "activity_bridge.py",
        "conversation_telemetry.py", "entrypoint.sh", "agent.yaml", "requirements.lock",
    ):
        try:
            hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            hashes[name] = "unavailable"
    names = (
        "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "MSI_ENDPOINT", "MSI_SECRET",
        "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET",
        "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST", "AZURE_TOKEN_CREDENTIALS",
        "SPEECH_KEY", "SPEECH_REGION", "SPEECH_RESOURCE_ID", "SPEECH_ENDPOINT",
    )
    return {
        "python": platform.python_version(), "packages": packages, "source_sha256": hashes,
        "env_present": {name: bool(os.environ.get(name)) for name in names},
        "configured_ids": {
            name: os.environ[name] for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID")
            if re.fullmatch(_GUID, os.environ.get(name, ""))
        },
        "endpoint_origins": {
            name: _origin(os.environ.get(name, ""))
            for name in ("PROJECT_ENDPOINT", "IDENTITY_ENDPOINT", "MSI_ENDPOINT", "SPEECH_ENDPOINT", "AZURE_AUTHORITY_HOST")
        },
    }


def _credential_factory():
    from azure.identity.aio import DefaultAzureCredential

    return DefaultAzureCredential(logging_enable=False)


async def _probe(run_id, target, scope):
    marker = _probing.set(True)
    credential = None
    started = time.monotonic()
    _emit(run_id, "token_probe_start", target=target, scope=scope)
    try:
        credential = _credential_factory()
        token = await asyncio.wait_for(credential.get_token(scope), TOKEN_TIMEOUT_SECONDS)
        _emit(run_id, "token_probe_result", target=target, scope=scope, status="success",
              elapsed_ms=round((time.monotonic() - started) * 1000),
              claims=_token_claims(token.token))
    except Exception as error:
        _emit(run_id, "token_probe_result", target=target, scope=scope,
              status="timeout" if isinstance(error, TimeoutError) else "error",
              elapsed_ms=round((time.monotonic() - started) * 1000), **_error_details(error))
    finally:
        try:
            if credential is not None:
                try:
                    await asyncio.wait_for(credential.close(), CLOSE_TIMEOUT_SECONDS)
                except Exception as error:
                    _emit(run_id, "credential_close_error", target=target, **_error_details(error))
        finally:
            _probing.reset(marker)


async def run_auth_diagnostics(*, speech_scope=None, speech_skip_reason="voice_disabled"):
    run_id = uuid.uuid4().hex
    log_filter = _ProbeLogFilter()
    handlers = set(logging.getLogger().handlers)
    for entry in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(entry, logging.Logger):
            handlers.update(entry.handlers)
    for handler in handlers:
        handler.addFilter(log_filter)
    try:
        _emit(run_id, "runtime", **_snapshot())
        if not (os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")):
            _emit(run_id, "probes_skipped", reason="no_hosted_identity_endpoint")
            return
        probes = [("foundry", FOUNDRY_SCOPE), ("storage", STORAGE_SCOPE)]
        if speech_scope:
            probes.append(("speech", speech_scope))
        else:
            _emit(run_id, "probe_skipped", target="speech", reason=speech_skip_reason)
        _emit(run_id, "probes_start", credential="independent_async_DefaultAzureCredential",
              timeout_seconds=TOKEN_TIMEOUT_SECONDS, service_access_tested=False)
        await asyncio.gather(*(_probe(run_id, target, scope) for target, scope in probes))
        _emit(run_id, "complete")
    except Exception as error:
        _emit(run_id, "diagnostic_error", **_error_details(error))
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)


def start_auth_diagnostics(*, speech_scope=None, speech_skip_reason="voice_disabled"):
    if os.environ.get("AUTH_DIAGNOSTICS_ENABLED", "false").strip().lower() not in ("true", "1", "yes", "on"):
        return None
    return asyncio.create_task(run_auth_diagnostics(
        speech_scope=speech_scope, speech_skip_reason=speech_skip_reason,
    ), name="auth-diagnostics")