<#
AgentBridge v2 - AVD clean install (retires the v1 bridge era on this box).

Run ON THE AVD, from inside the transfer pack folder built by
scripts/avd_move_pack.py (it drops a copy of this script into the pack):

    powershell -ExecutionPolicy Bypass -File .\avd_clean_install.ps1

What it does, in order:
  1. Stops v1: unregisters AgentBridge scheduled tasks, kills old
     bridge/worker python processes.
  2. Wipes LOCAL state: %USERPROFILE%\.agentbridge and the old repo clone.
     It never touches OneDrive/SharePoint synced folders (the v1 mesh and
     the mesh2 folder backup are shared data - a delete there syncs to
     every machine).
  3. Clones the v2 repo and installs the runtime with uv (all extras).
  4. Places the pack files (agent key + supabase.env) and writes a fresh
     config.json pointing at the cloud mesh.
  5. Starts the GUI once so the OWNER can sign in; adopts the agent to
     this machine over the local API; signs out and stops the GUI.
  6. Launches the harness and (optionally) registers a logon task so it
     survives reboots.
  7. Offers to delete the pack (it holds a plain key + cloud secrets).

PowerShell 5.1 compatible. Everything destructive is confirmed first.
#>

param(
    [string]$Agent    = "coco",
    [string]$RepoDir  = "$env:USERPROFILE\AgentBridge",
    [string]$RepoUrl  = "https://github.com/DAA-Aryan-Kumar/AgentBridge.git",
    [string]$MeshRoot = "supabase://mesh2",
    [int]$Port        = 7787
)

$ErrorActionPreference = "Stop"
$Home2 = Join-Path $env:USERPROFILE ".agentbridge"
$Pack  = $PSScriptRoot
$BaseUrl = "http://127.0.0.1:$Port"

function Say([string]$msg)  { Write-Host ""; Write-Host ">> $msg" -ForegroundColor Cyan }
function Warn([string]$msg) { Write-Host "!! $msg" -ForegroundColor Yellow }

Write-Host "AgentBridge v2 clean install  (agent: @$Agent, mesh: $MeshRoot)"
Write-Host "Pack folder: $Pack"
Write-Host ""
Write-Host "This will DELETE on this machine:"
Write-Host "  - any 'AgentBridge*' scheduled tasks and running v1 workers"
Write-Host "  - $Home2  (all v1-era local config/state)"
Write-Host "  - $RepoDir  (re-cloned fresh)"
Write-Host "It will NOT touch any OneDrive/SharePoint synced folder."
$go = Read-Host "Type WIPE to proceed"
if ($go -ne "WIPE") { Write-Host "Aborted."; exit 1 }

# --- sanity: the pack must hold the two files git cannot carry -------------
$KeySrc = Join-Path $Pack "keys\$Agent.key"
$EnvSrc = Join-Path $Pack "supabase.env"
if (-not (Test-Path $KeySrc)) { Warn "missing $KeySrc - build the pack with avd_move_pack.py"; exit 1 }
if (-not (Test-Path $EnvSrc)) { Warn "missing $EnvSrc - cloud creds must be in the pack"; exit 1 }

# --- 1. stop the v1 era ----------------------------------------------------
Say "Stopping v1 scheduled tasks + workers"
try {
    $tasks = Get-ScheduledTask | Where-Object { $_.TaskName -match "AgentBridge" }
    foreach ($t in $tasks) {
        Write-Host "  unregistering task: $($t.TaskName)"
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    }
} catch { Warn "scheduled-task sweep failed ($($_.Exception.Message)) - continue" }

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match "bridge|agentbridge|agent_worker" }
foreach ($p in $procs) {
    Write-Host "  killing PID $($p.ProcessId): $($p.CommandLine)"
    try { Stop-Process -Id $p.ProcessId -Force -Confirm:$false } catch { Warn "PID $($p.ProcessId) already gone" }
}

# --- 2. wipe local state ---------------------------------------------------
Say "Wiping local state"
if (Test-Path $Home2)   { Remove-Item $Home2 -Recurse -Force; Write-Host "  removed $Home2" }
if (Test-Path $RepoDir) { Remove-Item $RepoDir -Recurse -Force; Write-Host "  removed $RepoDir" }
$old = Read-Host "Path of any OTHER old v1 clone to delete (Enter to skip)"
if ($old -and (Test-Path $old)) {
    if ($old -match "OneDrive|SharePoint") {
        Warn "that path looks synced - refusing (deletes there propagate). Skipped."
    } else {
        Remove-Item $old -Recurse -Force; Write-Host "  removed $old"
    }
}

# --- 3. prerequisites + clone + runtime -------------------------------------
Say "Checking prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git is not installed. Install it (winget install --id Git.Git) and re-run."
    exit 1
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $ans = Read-Host "uv is not installed. Install it now from astral.sh? (y/n)"
    if ($ans -ne "y") { Warn "uv is required. Aborting."; exit 1 }
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Warn "uv still not on PATH - open a new shell and re-run."; exit 1
    }
}
if (-not (Get-Command cortex -ErrorAction SilentlyContinue)) {
    Warn "the 'cortex' CLI is not on PATH - @$Agent runs on the cortex adapter and will not run until it is installed. Continuing anyway."
}

Say "Cloning $RepoUrl"
git clone $RepoUrl $RepoDir
if (-not (Test-Path (Join-Path $RepoDir "pyproject.toml"))) { Warn "clone failed"; exit 1 }

Say "Installing the runtime (uv sync, all extras - first run downloads Python)"
Push-Location $RepoDir
uv sync --extra memory --extra mcp --extra cloud --extra retrieval
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Warn "uv sync failed (exit $rc)"; exit 1 }
$Py  = Join-Path $RepoDir ".venv\Scripts\python.exe"
$Pyw = Join-Path $RepoDir ".venv\Scripts\pythonw.exe"

# --- 4. home dir: key, creds, config ---------------------------------------
Say "Placing keys + config under $Home2"
New-Item -ItemType Directory -Force -Path (Join-Path $Home2 "keys") | Out-Null
Copy-Item $KeySrc (Join-Path $Home2 "keys\$Agent.key")
Copy-Item $EnvSrc (Join-Path $Home2 "supabase.env")
# ascii on purpose: PS 5.1 utf8 writes a BOM the json reader may reject
"{`"mesh_root`": `"$MeshRoot`"}" | Out-File -Encoding ascii (Join-Path $Home2 "config.json")
Write-Host "  keys\$Agent.key + supabase.env + config.json written"
Write-Host "  (the key file re-wraps itself with DPAPI on first load)"

# --- 5. adopt the agent to this machine ------------------------------------
Say "Starting the GUI server for the one-time adoption"
Start-Process $Pyw -ArgumentList "-m","agentbridge.gui","--no-browser" -WorkingDirectory $RepoDir
$up = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $null = Invoke-WebRequest "$BaseUrl/" -UseBasicParsing -TimeoutSec 2
        $up = $true; break
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $up) { Warn "GUI did not come up on $BaseUrl - check $RepoDir manually"; exit 1 }

Write-Host ""
Write-Host "Sign in ONCE as the agent's OWNER to adopt @$Agent here."
Write-Host "(Only the owner should ever sign in on this machine - signing in"
Write-Host " claims the machine's agents, by design.)"
$user = Read-Host "Owner username [aryan]"
if (-not $user) { $user = "aryan" }
$sec = Read-Host "Password for $user" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

try {
    $login = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/mesh/login" `
        -ContentType "application/json" `
        -Body (@{username = $user; password = $pw} | ConvertTo-Json)
} catch { Warn "login failed: $($_.Exception.Message)"; exit 1 }
$pw = $null
if ($login.error) { Warn "login failed: $($login.error)"; exit 1 }
Write-Host "  signed in as $user"

try {
    $adopt = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/mesh/adopt_agent" `
        -ContentType "application/json" -Body (@{agent = $Agent} | ConvertTo-Json)
} catch { Warn "adopt failed: $($_.Exception.Message)"; exit 1 }
if ($adopt.error) { Warn "adopt failed: $($adopt.error)"; exit 1 }
Write-Host "  @$Agent adopted to this machine ($env:COMPUTERNAME)"

$null = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/mesh/logout" -Body "{}" -ContentType "application/json"
$null = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/shutdown" -Body "{}" -ContentType "application/json"
Write-Host "  GUI stopped (this box only needs the harness)"

# --- 6. harness: launch now + start at logon --------------------------------
Say "Launching the harness"
Start-Process $Pyw -ArgumentList "-m","agentbridge.harness","--all" -WorkingDirectory $RepoDir
Start-Sleep -Seconds 5
$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match "agentbridge.harness" }
if ($alive) {
    Write-Host "  harness running (PIDs: $(($alive | ForEach-Object ProcessId) -join ', '))"
} else {
    Warn "harness not visible after 5s - check logs under $Home2\harness"
}

$ans = Read-Host "Register a logon task so the harness auto-starts? (y/n)"
if ($ans -eq "y") {
    $action  = New-ScheduledTaskAction -Execute $Pyw -Argument "-m agentbridge.harness --all" -WorkingDirectory $RepoDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Register-ScheduledTask -TaskName "AgentBridge Harness" -Action $action -Trigger $trigger -Description "AgentBridge v2 agent harness (--all)" | Out-Null
    Write-Host "  task 'AgentBridge Harness' registered (at logon)"
}

# --- 7. cleanup --------------------------------------------------------------
Write-Host ""
Write-Host "DONE. Verify from another machine: message @$Agent and watch it reply."
Write-Host "Then restart the harness on the OLD machine when convenient - it"
Write-Host "refuses agents homed elsewhere and will stand down for @$Agent."
$ans = Read-Host "Delete the transfer pack now? It holds a PLAIN key + secrets (type YES)"
if ($ans -eq "YES") {
    Set-Location $env:USERPROFILE
    Remove-Item $Pack -Recurse -Force
    Write-Host "  pack deleted. Delete the copy on the source machine too."
} else {
    Warn "pack kept - delete $Pack yourself once verified (and the source copy)."
}
