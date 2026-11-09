# Blender Agent sideload package

Teams and Microsoft 365 Copilot custom-engine app package, inspired by
[Foundry Charts](https://github.com/davrous/FoundryChartsAgent/tree/main/m365sideloadmanifest).
This package connects to the existing hosted agent; it does not deploy or change
Azure resources.

## Verified bot configuration

Read from Azure on November 9, 2026:

| Setting | Value |
|---|---|
| Azure Bot resource | `blender-agent-bot-ba85c04e` |
| Resource group | `davrousai-rg` |
| Display name | `blender-agent` |
| Bot Microsoft App ID | `27d67f5b-b7cc-4935-b979-0fdb47ccf365` |
| App type | `SingleTenant` |
| Microsoft Teams channel | `MsTeamsChannel`, enabled |
| Teams app ID | `22ce0d24-dea1-4bb2-b4c5-8a32bdae3902` |
| App version | `1.1.0` |

The configured messaging endpoint is:

```text
https://davrousfoundry.services.ai.azure.com/api/projects/proj-dav/agents/blender-agent/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview
```

The Teams app ID is a newly generated, stable package identity, **not** the Bot
Microsoft App ID. This installs a new Teams app backed by the existing bot.
To update an app you previously installed instead, first substitute that app's
existing manifest `id` and use a higher `version`. Keep `bots[].botId` and
`copilotAgents.customEngineAgents[].id` equal to the verified bot ID.

The endpoint and tenant configuration belong to Azure Bot, not the Teams
manifest. No secrets, SSO configuration, placeholder Application ID URI,
delegated Graph permissions or resource-specific consent permissions are added.
The bot remains single-tenant: this ZIP does not make it available in other
tenants.

## Branding and description

- [Original logo](icon-sources/logo.png): orange fantasy castle on a teal 3D
  cube, with a creative sparkle; an original mark, not the Blender logo.
- [Color icon](color.png): 192 x 192 PNG with an opaque navy background;
  the central artwork fits within the 120 x 120 safe area.
- [Outline icon](outline.png): 32 x 32 PNG, white-only visible pixels on a
  transparent background.
- [Manifest](manifest.json): complete English product description, capability
  limitations and cloud-processing disclaimer.

The publisher name and URLs follow the reference project's public publisher
metadata. **Privacy and terms currently point to the publisher's homepage**,
as in that reference. Replace them with actual published policy pages before
broader distribution or a Teams Store submission; the homepage is not a
project-specific privacy policy or terms document.

## Conversation starters

Six natural-language starters are declared in `bots[].commandLists`:

1. Create a fantasy castle.
2. Build a cozy village.
3. Render the current scene.
4. Find a 3D model.
5. Animate a camera orbit.
6. Download the Blender scene.

Each uses `type: "prompt"` with a complete `prompt`, not just a command label.
Teams inserts that text into the compose box when the user selects the starter.
They are scoped to personal chats, teams, group chats and Copilot. Client
versions and tenant policies determine their presentation. No unsupported
`conversation_starters` field or declarative-agent definition is added.

The animation starter explicitly excludes paid external processing. Payment
approval still requires the existing verified job controls, not a model turn.
To start over, send `/clear` in the relevant conversation.

## Build and validate

On Windows, from the repository root with PowerShell 7 and the repository's
`.venv` dependencies installed (including `jsonschema`):

```powershell
pwsh -NoProfile -File .\m365sideloadmanifest\build_package.ps1
```

The script downloads the official schema selected by the manifest (currently
`devPreview`), validates the manifest
with Python's Draft 4-compatible `jsonschema` validator,
regenerates both icons from the logo and outline geometry, and builds:

```text
m365sideloadmanifest\build\blender-agent.zip
```

The ZIP root contains exactly `manifest.json`, `color.png` and `outline.png`.
Do not upload a ZIP of this whole directory: sources, scripts and this README
are not part of the app package. Preserve app and bot identities and increment
the app `version` when distributing updates.

## Install in Teams

1. In Teams, select **Apps > Manage your apps > Upload an app > Upload a custom app**.
2. Upload [build/blender-agent.zip](build/blender-agent.zip).
3. Add it personally, to a team/channel or to a group chat as needed.
4. In a shared conversation, mention **@Blender Agent** and choose a starter.
   Keep follow-ups in the same thread, mentioning the agent when needed.

Custom app upload must be allowed by your tenant. The `copilot` scope and
custom-engine declaration advertise that surface but do not grant licenses or
override tenant availability. `supportsChannelFeatures: "tier1"` declares support
for the channel/thread experience. Native action updates in personal chats,
channels and group chats use the current Teams informative-update API; complete
results fall back to ordinary messages if the API refuses a stream.

The preview manifest retains `supportsTargetedMessages: true`. That property opts
in to receiving private targeted messages, not streaming. Private scene/history
isolation for targeted requests is not implemented by this status-delivery
change; use a personal chat for private Blender work. Preview features remain
subject to client and tenant availability.

`supportsFiles` advertises file support but does not add Graph access or remove
Teams restrictions: direct Teams file uploads are personal-chat-only; other
surfaces must deliver a supported attachment or download link to the agent.

Local schema, PNG and ZIP validation does not guarantee service-side acceptance.
The final check is installing the package in your tenant and sending messages.

## References

- [Official preview schema](https://developer.microsoft.com/en-us/json-schemas/teams/vdevPreview/MicrosoftTeams.schema.json)
- [Native informative updates](https://learn.microsoft.com/microsoftteams/platform/bots/streaming-ux)
- [Targeted messages](https://learn.microsoft.com/microsoftteams/platform/agents-in-teams/targeted-messages)
- [Prompt starters](https://learn.microsoft.com/microsoftteams/platform/bots/how-to/conversations/prompt-starters)
- [App packaging](https://learn.microsoft.com/microsoftteams/platform/concepts/build-and-test/apps-package)
- [Custom app upload](https://learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/apps-upload)
