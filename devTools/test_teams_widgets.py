"""Teams widget protocol, trusted exports, authorization and delivery regressions."""

import ast
import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activity_bridge as ab
import teams_widgets as widgets
from artifact_storage import conversation_scope
from test_activity_history import _Agent, _Context, _State, _patch_framework

ENV = {
    "TEAMS_WIDGET_ASSET_BASE_URL": "https://assets.example/teams-widget/v1",
    "AZURE_STORAGE_ACCOUNT_NAME": "widgettest",
}
ID = "a" * 32
SAS = "https://widgettest.blob.core.windows.net/screenshots/scenes/snapshot.glb?sig=temporary"
EXPORT = widgets.SceneExport(ID, "scenes/snapshot.glb", "widgettest", 128, "etag-1")


def context(conversation="19:widget;messageid=100"):
    value = _Context(conversation, "Export the scene as GLB.")
    value.activity.id = "user-100"
    value.activity.channel_id = ab.Channels.ms_teams
    value.activity.channel_data = {"tenant": {"id": "tenant-a"}}
    value.activity.is_agentic_request = lambda: False
    return value


def scope(value, generation=0):
    return conversation_scope(ab._scene_key(value.activity.conversation.id, generation))


def records(value):
    return widgets.remember(EXPORT, scope(value), widgets.context_binding(value), 0, [])


def storage():
    properties = SimpleNamespace(size=128, etag="etag-1")
    return SimpleNamespace(
        blob=Mock(return_value=SimpleNamespace(get_blob_properties=Mock(return_value=properties))),
        signed_url=Mock(return_value=SAS),
    )


def connector(value, sent=None):
    send = sent or AsyncMock(return_value=SimpleNamespace(id="native-status"))
    client = SimpleNamespace(conversations=SimpleNamespace(send_to_conversation=send), close=AsyncMock())
    value.turn_state["factory"] = SimpleNamespace(create_connector_client=AsyncMock(return_value=client))
    return send


class ExportAgent(_Agent):
    def __init__(self, *, gate=None, started=None, forged=False):
        super().__init__()
        self.gate, self.started, self.forged = gate, started, forged

    def run(self, _messages, **_kwargs):
        async def generate():
            yield SimpleNamespace(
                message_id="status-export_scene_as_glb_for_download",
                contents=[SimpleNamespace(type="text", text="Exporting scene...")],
            )
            if not self.forged:
                # The tool upload helper executes in a copied worker context.
                await asyncio.to_thread(widgets.record_export, EXPORT.blob, EXPORT.account, EXPORT.size, EXPORT.etag)
            if self.started:
                self.started.set()
            if self.gate:
                await self.gate.wait()
            yield SimpleNamespace(
                message_id="tool-link-glb",
                contents=[SimpleNamespace(type="text", text=f"[Download GLB]({SAS})\n\n")],
            )
            yield SimpleNamespace(message_id="final", contents=[SimpleNamespace(type="text", text="Exported scene.")])
        return generate()


class WidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _patch_framework()
        ab._conversations.clear()
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.interval = patch.object(ab, "_TEAMS_STATUS_INTERVAL", 0)
        self.interval.start()
        self.detach = patch.object(ab, "_PROACTIVE_AFTER_SECONDS", 0)
        self.detach.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.interval.stop)
        self.addCleanup(self.detach.stop)

    def test_configuration_and_safe_html(self):
        config = widgets.configuration()
        text = widgets.markdown(ID, config)
        payload = json.loads(text.split("\n")[1])
        self.assertEqual(payload["type"], "widget/mcp-ui")
        self.assertEqual(payload["correlationId"], ID)
        self.assertEqual(payload["securityPolicy"]["connectDomains"],
                         ["https://assets.example", "https://widgettest.blob.core.windows.net"])
        self.assertIn("https://assets.example/teams-widget/v1/widget.js", payload["html"])
        self.assertNotIn("sig=", payload["html"])
        self.assertNotIn("/api/blob", payload["html"])
        self.assertNotIn("/api/assets", payload["html"])
        with patch.dict(os.environ, {"TEAMS_WIDGET_ASSET_BASE_URL": ""}):
            self.assertIsNone(widgets.configuration())
        for invalid in ("http://example.org/v1", "https://user:secret@example.org", "https://example.org?sig=x"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {"TEAMS_WIDGET_ASSET_BASE_URL": invalid}):
                with self.assertRaises(ValueError):
                    widgets.configuration()

    def test_upload_helper_is_the_trusted_producer(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        upload = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upload_glb_to_blob")
        fake = storage()
        fake.account = "widgettest"
        fake.upload = Mock(return_value={"etag": "etag-1"})
        namespace = {"os": os, "ArtifactStorage": lambda _account: fake,
                     "AZURE_STORAGE_ACCOUNT_NAME": "widgettest", "logger": logging.getLogger("widget-test")}
        exec(compile(ast.Module(body=[upload], type_ignores=[]), "main.py", "exec"), namespace)
        captured = []
        token = widgets.exports.set(captured)
        try:
            with tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / "scene.glb"
                file.write_bytes(b"x" * 128)
                self.assertEqual(namespace["upload_glb_to_blob"](str(file), EXPORT.blob), SAS)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].blob, EXPORT.blob)
                self.assertEqual(captured[0].etag, EXPORT.etag)
                fake.signed_url.side_effect = RuntimeError("no delegation")
                with self.assertRaises(RuntimeError):
                    namespace["upload_glb_to_blob"](str(file), EXPORT.blob)
                self.assertEqual(len(captured), 1, "Failed downloads must not register another widget.")
        finally:
            widgets.exports.reset(token)

    def test_registry_is_bounded_and_invalid_state_fails_closed(self):
        value = context()
        registry = []
        for index in range(widgets.MAX_WIDGETS + 4):
            export = widgets.SceneExport(f"{index:032x}", EXPORT.blob, EXPORT.account, EXPORT.size, EXPORT.etag)
            registry = widgets.remember(export, scope(value), widgets.context_binding(value), 0, registry)
        self.assertEqual(len(registry), widgets.MAX_WIDGETS)
        self.assertEqual(registry[0]["id"], f"{4:032x}")
        state = _State({widgets.STATE_KEY: ["invalid"]})
        with self.assertLogs("blender_agent.activity", level="WARNING"):
            self.assertEqual(ab._load_scene_widgets(state, ab._conversation_slot("corrupt")), [])

    async def test_authorized_renewal_and_ping(self):
        value = context()
        fake = storage()
        with patch("artifact_storage.ArtifactStorage", return_value=fake):
            for operation in ("scene_widget_ping", "load_scene_snapshot", "load_scene_snapshot"):
                value.activity.value = {"name": operation, "arguments": {"id": ID, "__msteams__": {"ignored": True}}}
                result = await widgets.call_tool(value, records(value), scope(value), 0, lambda: True)
                body = result["callToolResult"]
                self.assertEqual(result["responseType"], "htmlwidget/calltoolresult")
                self.assertEqual(set(body), {"content", "structuredContent", "isError"})
                self.assertFalse(body["isError"])
                self.assertEqual(body["structuredContent"]["id"], ID)
                if operation == "scene_widget_ping":
                    fake.signed_url.assert_not_called()
                else:
                    self.assertEqual(body["structuredContent"]["url"], SAS)
            self.assertEqual(fake.signed_url.call_count, 2)

    async def test_rejects_untrusted_invokes_without_accessing_storage(self):
        owner = context()
        registry = records(owner)
        for case in ("thread", "tenant", "bot", "anonymous", "channel", "generation", "unknown", "bad-id", "mutation"):
            value = context()
            value.activity.value = {"name": "load_scene_snapshot", "arguments": {"id": ID, "url": SAS}}
            generation = 0
            if case == "thread":
                value.activity.conversation.id = "19:other-thread;messageid=200"
            elif case == "tenant":
                value.activity.channel_data["tenant"]["id"] = "tenant-b"
            elif case == "bot":
                value.activity.recipient.id = "28:other-bot"
            elif case == "anonymous":
                value.identity.is_authenticated = False
            elif case == "channel":
                value.activity.channel_id = "emulator"
            elif case == "generation":
                generation = 1
            elif case == "unknown":
                value.activity.value["arguments"]["id"] = "b" * 32
            elif case == "bad-id":
                value.activity.value["arguments"]["id"] = "../../private"
            elif case == "mutation":
                value.activity.value["name"] = "submit_seedance_job"
            with self.subTest(case=case), patch("artifact_storage.ArtifactStorage") as factory:
                result = await widgets.call_tool(value, registry, scope(value, generation), generation, lambda: True)
                self.assertTrue(result["callToolResult"]["isError"])
                self.assertNotIn("sig=", json.dumps(result))
                factory.assert_not_called()

    async def test_unavailable_changed_oversized_and_revoked_snapshots(self):
        value = context()
        value.activity.value = {"name": "load_scene_snapshot", "arguments": {"id": ID}}
        for case in ("deleted", "changed", "oversized", "clear-during-load"):
            fake = storage()
            current = [True]
            head = fake.blob.return_value.get_blob_properties
            if case == "deleted":
                head.side_effect = RuntimeError("signed URL must never appear in errors: " + SAS)
            elif case == "changed":
                head.return_value.etag = "changed"
            elif case == "oversized":
                head.return_value.size = widgets.MAX_GLB_BYTES + 1
            else:
                def revoke(*_args, **_kwargs):
                    current[0] = False
                    return SAS
                fake.signed_url.side_effect = revoke
            with self.subTest(case=case), patch("artifact_storage.ArtifactStorage", return_value=fake):
                result = await widgets.call_tool(value, records(value), scope(value), 0, lambda: current[0])
                self.assertTrue(result["callToolResult"]["isError"])
                self.assertNotIn("sig=", json.dumps(result))

    async def test_complete_widget_after_native_final_and_cold_restore(self):
        value = context()
        state = _State({})
        send = connector(value)
        await ab._run_turn(ExportAgent(), value, state)
        delivered = [call.args[1] for call in send.await_args_list]
        self.assertEqual([item.type for item in delivered], ["typing", "message", "message"])
        self.assertEqual(delivered[-2].entities[0]["streamType"], "final")
        self.assertIn("Exported scene.", delivered[-2].text)
        self.assertIn(SAS, delivered[-2].text, "Existing download remains intact.")
        self.assertEqual(delivered[-1].text_format, "extendedmarkdown")
        self.assertNotIn("sig=", delivered[-1].text)
        self.assertEqual({item.reply_to_id for item in delivered}, {"user-100"})
        saved = json.loads(json.dumps(state.conversation._backing))
        ab._conversations.clear()
        restored = ab._load_scene_widgets(_State(saved), ab._conversation_slot(value.activity.conversation.id))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["blob"], EXPORT.blob)
        stale = _State({})
        await ab._run_turn(ExportAgent(), value, stale)
        self.assertEqual(len(stale.conversation.get_value(widgets.STATE_KEY)), 2, "Queued state snapshots use the authoritative registry.")

    async def test_registry_survives_recreating_durable_storage(self):
        from microsoft_agents.hosting.core.storage import StoreItem

        class Item(StoreItem):
            def __init__(self, value):
                self.value = value
            def store_item_to_json(self):
                return self.value
            @staticmethod
            def from_json_to_store_item(value):
                return Item(value)

        stored = {widgets.STATE_KEY: records(context())}
        with tempfile.TemporaryDirectory() as directory, patch.object(ab.os.path, "expanduser", return_value=directory):
            first = ab._build_storage()
            await first.write({"widget-test": Item(stored)})
            self.assertTrue((Path(directory) / ".blender_activity_state.json").exists())
            restarted = ab._build_storage()
            restored = await restarted.read(["widget-test"], target_cls=Item)
            self.assertEqual(restored["widget-test"].value[widgets.STATE_KEY][0]["id"], ID)

    async def test_disabled_no_export_and_forged_links_keep_existing_behavior(self):
        for mode in ("disabled", "no-export", "forged"):
            value = context(mode)
            send = connector(value)
            env = {"TEAMS_WIDGET_ASSET_BASE_URL": ""} if mode == "disabled" else ENV
            with self.subTest(mode=mode), patch.dict(os.environ, env):
                await ab._run_turn(_Agent() if mode == "no-export" else ExportAgent(forged=mode == "forged"), value, _State({}))
                self.assertFalse(any(getattr(call.args[1], "text_format", None) == "extendedmarkdown"
                                     for call in send.await_args_list))

    async def test_detached_delivery_persists_before_using_fresh_connector(self):
        value = context()
        state = _State({})
        gate, started, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def post(_conversation, activity):
            if getattr(activity, "text_format", None) == "extendedmarkdown":
                self.assertGreater(state.saved, 0)
                delivered.set()
            return SimpleNamespace(id="native-status")

        connector(value, AsyncMock(side_effect=post))
        with patch.object(ab, "_PROACTIVE_AFTER_SECONDS", 0.001):
            await ab._run_turn(ExportAgent(gate=gate, started=started), value, state)
            await asyncio.wait_for(started.wait(), 2)
            value.send_activity = AsyncMock(side_effect=RuntimeError("Inbound context closed"))
            gate.set()
            await asyncio.wait_for(delivered.wait(), 2)
        value.send_activity.assert_not_called()

    async def test_clear_discards_inflight_widget_and_persisted_registry(self):
        value, state = context(), _State({})
        gate, started = asyncio.Event(), asyncio.Event()
        send = connector(value)
        task = asyncio.create_task(ab._run_turn(ExportAgent(gate=gate, started=started), value, state))
        await asyncio.wait_for(started.wait(), 2)
        slot = ab._conversation_slot(value.activity.conversation.id)
        ab._clear_conversation(state, slot)
        gate.set()
        await task
        self.assertEqual(state.conversation.get_value(widgets.STATE_KEY), [])
        self.assertFalse(any(item.args[1].type == "message" for item in send.await_args_list))
        ab._conversations.clear()
        restored = ab._conversation_slot(value.activity.conversation.id)
        self.assertEqual(ab._load_scene_widgets(state, restored), [])

    async def test_precise_invoke_route_and_sdk_wire_contract(self):
        class App:
            def __init__(self):
                self.routes = []
            def add_route(self, selector, handler, **kwargs):
                self.routes.append((selector, handler, kwargs))
            def activity(self, *_args, **_kwargs):
                return lambda handler: handler
            conversation_update = activity
            def error(self, handler):
                return handler

        app = App()
        ab.register_activity_handlers(SimpleNamespace(agent_app=app), _Agent())
        selector, handler, options = app.routes[0]
        value = context()
        value.activity.type = "invoke"
        value.activity.name = widgets.INVOKE_NAME
        value.activity.value = {"name": "scene_widget_ping", "arguments": {"id": ID}}
        self.assertTrue(selector(value))
        self.assertTrue(options["is_invoke"])
        state = _State({widgets.STATE_KEY: records(value)})
        await handler(value, state)
        reply = value.sent[-1]
        self.assertEqual(reply.type, "invokeResponse")
        self.assertEqual(reply.value["status"], 200)
        self.assertFalse(reply.value["body"]["callToolResult"]["isError"])
        for name in ("adaptiveCard/action", "signin/verifyState", ""):
            value.activity.name = name
            self.assertFalse(selector(value))
        from microsoft_agents.activity import Activity
        wire = Activity(type="invokeResponse", value=reply.value).model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(wire["value"]["body"]["responseType"], "htmlwidget/calltoolresult")
        message = Activity(type="message", text="widget", text_format="extendedmarkdown")
        self.assertEqual(message.model_dump(by_alias=True)["textFormat"], "extendedmarkdown")


if __name__ == "__main__":
    unittest.main()
