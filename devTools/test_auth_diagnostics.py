"""Offline authentication diagnostic checks: python devTools/test_auth_diagnostics.py."""

import asyncio
import base64
import io
import json
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth_diagnostics as diagnostics


PRINCIPAL = "020d1fb4-dee5-4d83-a95b-08ac93340924"
CORRELATION = "d27dada8-4b3a-46e4-942d-018e907d6947"
POLICY = "3d79d567-88b8-4901-ae54-01418818a0e8"
SECRET = "NEVER_LOG_THIS_SECRET"


def _token():
    payload = base64.urlsafe_b64encode(json.dumps({
        "oid": PRINCIPAL, "aud": "https://storage.azure.com/",
        "upn": SECRET, "name": SECRET, "roles": [SECRET], "exp": 1900000000,
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.SECRET_SIGNATURE"


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.parent = logging.getLogger("blender_agent")
        self.old_level = self.parent.level
        self.parent.setLevel(logging.INFO)
        self.parent.addHandler(self.handler)
        self.sdk_logger = logging.getLogger("diagnostic_test_sdk")
        self.sdk_logger.addHandler(self.handler)
        self.sdk_logger.setLevel(logging.WARNING)
        self.credentials = []

    def tearDown(self):
        self.parent.removeHandler(self.handler)
        self.parent.setLevel(self.old_level)
        self.sdk_logger.removeHandler(self.handler)

    def factory(self):
        credential = SimpleNamespace(closed=False)

        async def get_token(scope):
            self.sdk_logger.warning(SECRET)
            await asyncio.sleep(0)
            if scope == diagnostics.FOUNDRY_SCOPE:
                raise RuntimeError(
                    f'AADSTS53003 invalid_grant Correlation ID: {CORRELATION} '
                    f'"capolids":{{"values":["{POLICY}"]}} token={SECRET}'
                )
            if scope == "https://cognitiveservices.azure.com/.default":
                raise RuntimeError(f"invalid_scope {SECRET}")
            return SimpleNamespace(token=_token())

        async def close():
            credential.closed = True

        credential.get_token = get_token
        credential.close = close
        self.credentials.append(credential)
        return credential

    def events(self):
        return [json.loads(line.split("AUTH_DIAG ", 1)[1])
                for line in self.output.getvalue().splitlines() if "AUTH_DIAG " in line]

    async def test_independent_failures_and_redaction(self):
        with patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://localhost/identity"}, clear=True), \
                patch.object(diagnostics, "_credential_factory", self.factory), \
                patch.object(diagnostics, "_snapshot", return_value={}):
            await asyncio.gather(
                diagnostics.run_auth_diagnostics(speech_scope="https://cognitiveservices.azure.com/.default"),
                self.unrelated_log(),
            )
        results = {event["target"]: event for event in self.events() if event["event"] == "token_probe_result"}
        self.assertEqual(results["foundry"]["aadsts_codes"], ["AADSTS53003"])
        self.assertEqual(results["foundry"]["correlation_ids"], [CORRELATION])
        self.assertEqual(results["foundry"]["policy_ids"], [POLICY])
        self.assertEqual(results["speech"]["oauth_errors"], ["invalid_scope"])
        self.assertEqual(results["storage"]["status"], "success")
        self.assertEqual(results["storage"]["claims"]["oid"], PRINCIPAL)
        self.assertNotIn(SECRET, self.output.getvalue())
        self.assertNotIn(_token(), self.output.getvalue())
        self.assertIn("unrelated request remains visible", self.output.getvalue())
        self.assertTrue(all(credential.closed for credential in self.credentials))
        self.assertEqual(self.handler.filters, [])

    async def unrelated_log(self):
        await asyncio.sleep(0)
        self.sdk_logger.warning("unrelated request remains visible")

    async def test_disabled_and_local_do_not_acquire_tokens(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(diagnostics, "_credential_factory") as factory, \
                patch.object(diagnostics, "_snapshot", return_value={}):
            self.assertIsNone(diagnostics.start_auth_diagnostics())
            await diagnostics.run_auth_diagnostics()
            factory.assert_not_called()
        self.assertEqual(self.events()[-1]["reason"], "no_hosted_identity_endpoint")

    async def test_speech_disabled_still_probes_foundry_and_storage(self):
        with patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://localhost"}, clear=True), \
                patch.object(diagnostics, "_credential_factory", self.factory), \
                patch.object(diagnostics, "_snapshot", return_value={}):
            await diagnostics.run_auth_diagnostics()
        results = [event for event in self.events() if event["event"] == "token_probe_result"]
        self.assertEqual({event["target"] for event in results}, {"foundry", "storage"})
        self.assertEqual(len(self.credentials), 2)

    async def test_enabled_start_returns_background_task(self):
        entered = asyncio.Event()

        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.dict(os.environ, {"AUTH_DIAGNOSTICS_ENABLED": "true"}, clear=True), \
                patch.object(diagnostics, "run_auth_diagnostics", blocked):
            task = diagnostics.start_auth_diagnostics()
            self.assertIsInstance(task, asyncio.Task)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_timeout_and_cancellation_close_credentials(self):
        credential = self.factory()

        async def blocked(*args):
            await asyncio.Event().wait()

        credential.get_token = blocked
        with patch.object(diagnostics, "_credential_factory", return_value=credential), \
                patch.object(diagnostics, "TOKEN_TIMEOUT_SECONDS", 0.01):
            await diagnostics._probe("test", "foundry", diagnostics.FOUNDRY_SCOPE)
            self.assertEqual(self.events()[-1]["status"], "timeout")
            self.assertTrue(credential.closed)
            credential.closed = False
            task = asyncio.create_task(diagnostics._probe("test", "foundry", diagnostics.FOUNDRY_SCOPE))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(credential.closed)

    def test_snapshot_never_records_secret_environment_values(self):
        with patch.dict(os.environ, {
            "IDENTITY_HEADER": SECRET, "AZURE_CLIENT_SECRET": SECRET, "SPEECH_KEY": SECRET,
            "IDENTITY_ENDPOINT": f"http://user:{SECRET}@localhost:8081/{SECRET}?sig={SECRET}",
            "AZURE_CLIENT_ID": PRINCIPAL,
        }, clear=True):
            snapshot = diagnostics._snapshot()
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assertTrue(snapshot["env_present"]["IDENTITY_HEADER"])
        self.assertEqual(snapshot["configured_ids"]["AZURE_CLIENT_ID"], PRINCIPAL)
        self.assertEqual(snapshot["endpoint_origins"]["IDENTITY_ENDPOINT"], "http://localhost:8081")
        self.assertEqual(len(snapshot["source_sha256"]["main.py"]), 64)

    def test_opaque_token_and_malformed_endpoints(self):
        for token in (SECRET, "header.!@#.signature", "header.W10.signature"):
            self.assertEqual(diagnostics._token_claims(token), {"format": "opaque"})
        self.assertIsNone(diagnostics._origin("http://localhost:invalid"))


if __name__ == "__main__":
    unittest.main()