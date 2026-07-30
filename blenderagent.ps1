#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Build and run the Blender scene agent container.

.DESCRIPTION
    Windows / PowerShell counterpart of the `blenderagent` bash script. Both
    accept the same verbs, so the muscle memory is identical on either OS:

        .\blenderagent.ps1 rebuild     # docker build
        .\blenderagent.ps1 start       # docker run
        .\blenderagent.ps1 up          # rebuild, then start
        .\blenderagent.ps1 playground  # M365 Agents Playground, pointed at the container

    Anything after the verb is passed straight through to the underlying tool, e.g.

        .\blenderagent.ps1 rebuild --no-cache
        .\blenderagent.ps1 start -e LOG_LEVEL=DEBUG

.NOTES
    Runs from the script's own directory, so it works from anywhere.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('rebuild', 'start', 'up', 'playground', 'help')]
    [string]$Command = 'help',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Image = 'blender-scene-agent'
$Root = $PSScriptRoot

# Where the Playground POSTs activities, and where the agent posts replies back.
# `host.docker.internal` (not localhost) because the agent runs in the container:
# localhost there is the container itself, so replies would never reach us.
$PlaygroundEndpoint = 'http://localhost:8088/api/messages'
$PlaygroundServiceUrl = 'http://host.docker.internal:56150/_connector'

function Show-Help {
    @"
blenderagent — build and run the Blender scene agent container

  .\blenderagent.ps1 rebuild    [docker args]   Build the image ($Image)
  .\blenderagent.ps1 start      [docker args]   Run it on http://localhost:8088
  .\blenderagent.ps1 up         [docker args]   Rebuild, then start
  .\blenderagent.ps1 playground [pg args]       Open the M365 Agents Playground
  .\blenderagent.ps1 help                       This message

Examples
  .\blenderagent.ps1 rebuild --no-cache
  .\blenderagent.ps1 start -e LOG_LEVEL=DEBUG

The container must already be running (in another terminal) before 'playground'.
"@ | Write-Host
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker was not found on PATH. Install Docker Desktop and make sure it is running."
    }
}

function Invoke-Docker {
    param([string[]]$Arguments)
    Write-Host "> docker $($Arguments -join ' ')" -ForegroundColor DarkGray
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-Rebuild {
    Assert-Docker
    # --platform linux/amd64 is required: the image is built for the Foundry
    # hosted-agent runtime, so an Apple Silicon host must cross-build rather
    # than produce an arm64 image that will not run once deployed.
    Invoke-Docker (@('build', '--platform', 'linux/amd64', '-t', $Image) + $DockerArgs + @('.'))
}

function Invoke-Start {
    Assert-Docker

    $envFile = Join-Path $Root '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw ".env not found in $Root. Copy .env.example to .env and fill it in first."
    }

    # Mounted read-only so the container's DefaultAzureCredential can reuse the
    # `az login` tokens from the host instead of needing its own sign-in.
    $azureDir = Join-Path $HOME '.azure'
    if (-not (Test-Path -LiteralPath $azureDir)) {
        Write-Warning "$azureDir does not exist — run 'az login' first or the agent will fail to authenticate."
    }
    # Docker wants forward slashes even on Windows.
    $azureMount = ($azureDir -replace '\\', '/') + ':/root/.azure:ro'

    Invoke-Docker (@(
            'run', '-it', '--rm',
            '-p', '8088:8088',
            '-p', '8089:8089',
            '--env-file', '.env',
            '-v', $azureMount
        ) + $DockerArgs + @($Image))
}

function Invoke-Playground {
    if (-not (Get-Command agentsplayground -ErrorAction SilentlyContinue)) {
        throw "agentsplayground was not found on PATH. Install it with 'winget install agentsplayground'."
    }

    # The Playground only drives an already-running agent; it does not start one.
    try {
        $null = Invoke-WebRequest -Uri $PlaygroundEndpoint -Method Post -Body '{}' `
            -ContentType 'application/json' -TimeoutSec 2 -ErrorAction Stop
    }
    catch [System.Net.WebException], [Microsoft.PowerShell.Commands.HttpResponseException] {
        # A 4xx means something IS listening, which is all we wanted to know.
    }
    catch {
        Write-Warning "Nothing answered on http://localhost:8088 - run '.\blenderagent.ps1 start' in another terminal first."
    }

    $arguments = @('-e', $PlaygroundEndpoint, '--service-url', $PlaygroundServiceUrl) + $DockerArgs
    Write-Host "> agentsplayground $($arguments -join ' ')" -ForegroundColor DarkGray
    & agentsplayground @arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Push-Location $Root
try {
    switch ($Command) {
        'rebuild' { Invoke-Rebuild }
        'start' { Invoke-Start }
        'up' { Invoke-Rebuild; Invoke-Start }
        'playground' { Invoke-Playground }
        default { Show-Help }
    }
}
finally {
    Pop-Location
}
