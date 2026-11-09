#requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$manifestPath = Join-Path $PSScriptRoot 'manifest.json'
$manifestText = Get-Content -LiteralPath $manifestPath -Raw
$manifest = $manifestText | ConvertFrom-Json
$schema = Invoke-WebRequest -Uri $manifest.'$schema'
$python = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create the repository .venv and install its requirements before building the package.'
}
# The official schema uses Draft 4, which PowerShell Test-Json cannot fully parse.
$validation = @'
import json
import sys
import jsonschema

schema = json.load(sys.stdin)
validator = jsonschema.validators.validator_for(schema)
with open(sys.argv[1], encoding="utf-8") as source:
    validator(schema, format_checker=jsonschema.FormatChecker()).validate(json.load(source))
print("Official Teams schema validation passed.")
'@
$schema.Content | & $python -c $validation $manifestPath
if ($LASTEXITCODE -ne 0) {
    throw 'Manifest validation failed. Check the Python validator output above.'
}
if ($manifest.id -eq $manifest.bots[0].botId) {
    throw 'The Teams app ID must be independent of the Azure Bot app ID.'
}
if ($manifest.copilotAgents.customEngineAgents[0].id -ne $manifest.bots[0].botId) {
    throw 'The custom-engine agent ID must match the Azure Bot app ID.'
}
foreach ($command in $manifest.bots[0].commandLists[0].commands) {
    if ($command.type -ne 'prompt' -or [string]::IsNullOrWhiteSpace($command.prompt)) {
        throw "Missing prompt-starter configuration: $($command.title)"
    }
}

$source = [System.Drawing.Image]::FromFile((Join-Path $PSScriptRoot 'icon-sources\logo.png'))
$color = [System.Drawing.Bitmap]::new(192, 192)
$graphics = [System.Drawing.Graphics]::FromImage($color)
$attributes = [System.Drawing.Imaging.ImageAttributes]::new()
try {
    $graphics.Clear([System.Drawing.ColorTranslator]::FromHtml($manifest.accentColor))
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    # Remove the generated navy backdrop before placing the mark in the safe area.
    $attributes.SetColorKey(
        [System.Drawing.Color]::FromArgb(0, 0, 0),
        [System.Drawing.Color]::FromArgb(45, 65, 125)
    )
    $graphics.DrawImage($source, [System.Drawing.Rectangle]::new(16, 16, 160, 160),
        0, 0, $source.Width, $source.Height, [System.Drawing.GraphicsUnit]::Pixel, $attributes)
    $color.Save((Join-Path $PSScriptRoot $manifest.icons.color), [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    $attributes.Dispose()
    $graphics.Dispose()
    $color.Dispose()
    $source.Dispose()
}

$outline = [System.Drawing.Bitmap]::new(32, 32)
$graphics = [System.Drawing.Graphics]::FromImage($outline)
$pen = [System.Drawing.Pen]::new([System.Drawing.Color]::White, 1.5)
$path = [System.Drawing.Drawing2D.GraphicsPath]::new()
try {
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $path.AddLines([System.Drawing.PointF[]]@(
        [System.Drawing.PointF]::new(4, 17), [System.Drawing.PointF]::new(16, 22),
        [System.Drawing.PointF]::new(28, 17), [System.Drawing.PointF]::new(28, 25),
        [System.Drawing.PointF]::new(16, 30), [System.Drawing.PointF]::new(4, 25),
        [System.Drawing.PointF]::new(4, 17), [System.Drawing.PointF]::new(4, 12),
        [System.Drawing.PointF]::new(7, 13), [System.Drawing.PointF]::new(7, 16),
        [System.Drawing.PointF]::new(10, 15), [System.Drawing.PointF]::new(10, 9),
        [System.Drawing.PointF]::new(8, 9), [System.Drawing.PointF]::new(15, 2),
        [System.Drawing.PointF]::new(22, 9), [System.Drawing.PointF]::new(20, 9),
        [System.Drawing.PointF]::new(20, 16), [System.Drawing.PointF]::new(23, 15),
        [System.Drawing.PointF]::new(23, 12), [System.Drawing.PointF]::new(28, 14),
        [System.Drawing.PointF]::new(28, 17)
    ))
    $graphics.DrawPath($pen, $path)
    $graphics.DrawLine($pen, 16, 22, 16, 30)
    $graphics.DrawLine($pen, 25, 2, 25, 8)
    $graphics.DrawLine($pen, 22, 5, 28, 5)
    # Keep antialiasing in alpha, with white RGB even on partially visible pixels.
    for ($y = 0; $y -lt 32; $y++) {
        for ($x = 0; $x -lt 32; $x++) {
            $alpha = $outline.GetPixel($x, $y).A
            $outline.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($alpha, 255, 255, 255))
        }
    }
    $outline.Save((Join-Path $PSScriptRoot $manifest.icons.outline), [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    $path.Dispose()
    $pen.Dispose()
    $graphics.Dispose()
    $outline.Dispose()
}

$files = @('manifest.json', $manifest.icons.color, $manifest.icons.outline)
$build = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Path $build -Force | Out-Null
$zipPath = Join-Path $build 'blender-agent.zip'
Compress-Archive -LiteralPath ($files | ForEach-Object { Join-Path $PSScriptRoot $_ }) -DestinationPath $zipPath -Force
$archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($archive.Entries.FullName | Sort-Object)
    if (@(Compare-Object ($files | Sort-Object) $entries).Count -ne 0) {
        throw 'Unexpected ZIP contents: only the manifest and its two icons are allowed.'
    }
}
finally {
    $archive.Dispose()
}
Write-Host "Validated manifest and created $zipPath"
