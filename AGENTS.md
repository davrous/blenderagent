# Repository guidance

## Microsoft Foundry

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first.
If you are in VS Code, read the vscode-microsoft-foundry skill first.

The container composes Responses, Activity, and `invocations_ws` protocols on one Agent Server host. Preserve cooperative host inheritance and register voice through the invocations SDK's `ws_handler`.

Typed and voice turns share one Foundry Responses conversation in hosted mode. Keep `agent_session_id` for sandbox affinity and `conversation` for transcript continuity; they are not interchangeable.

## Focused checks

```bash
python devTools/test_voice_pipeline.py
python devTools/test_activity_history.py
cd webchat && npm run build
node ../devTools/test_voice_relay.mjs
```

Video pipeline checks: `.venv/Scripts/python.exe devTools/test_video_pipeline.py`
and `node devTools/test_video_webchat.mjs` (from the repository root).
Paid Seedance submission is only authorized by verified channel controls, never
by a model tool call. Keep ambiguous submissions fail-closed and preserve the
scene snapshot/Blob lease recovery path. A background thread cannot prevent hosted idle.
