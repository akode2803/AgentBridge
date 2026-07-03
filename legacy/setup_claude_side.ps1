# One-time Claude-side setup, run AFTER the EB OneDrive shortcut has synced locally.
# Finds the synced "AgentBridge (AK)" folder, points the bridge at it, publishes the
# app into shared bin/ (so the remote side can bootstrap), and sends the hello ping.
#
#   powershell -ExecutionPolicy Bypass -File setup_claude_side.ps1
#   (or pass the folder explicitly:  ... -Shared "C:\Users\me\OneDrive - EmployBridge\AgentBridge (AK)")

param([string]$Shared)

if (-not $Shared) {
    $hits = Get-ChildItem "$env:USERPROFILE\OneDrive*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ChildItem $_.FullName -Directory -Filter "*AgentBridge*" -ErrorAction SilentlyContinue }
    # OneDrive names shared shortcuts "<owner>'s files - <folder>", hence the leading wildcard
    # prefer the EmployBridge tenant if several OneDrives contain a match
    $hits = @($hits | Sort-Object { $_.FullName -notmatch "Employ" })
    if (-not $hits) {
        Write-Host "No synced 'AgentBridge*' folder found under $env:USERPROFILE\OneDrive*."
        Write-Host "Finish the OneDrive 'Add shortcut to My files' step first (see README), wait for sync, then re-run."
        exit 1
    }
    $Shared = $hits[0].FullName
}

Write-Host "Using shared folder: $Shared"
$bridge = Join-Path $PSScriptRoot "bridge.py"
python $bridge init --role claude --shared "$Shared"
python $bridge doctor
if (-not $?) { Write-Host "doctor reported issues - fix before continuing"; exit 1 }
python $bridge publish
python $bridge send --body-file (Join-Path $PSScriptRoot "hello_coco.md") --type ping
python $bridge status
Write-Host ""
Write-Host "Claude side is live. Next: run REMOTE_SETUP.md on the remote machine."
