# AI DevTeam on Windows with WSL containers (wslc). wslc has no `compose` yet, so this script does
# what docker-compose.yml does: one network, three volumes, six containers, each with ONLY its own
# secrets (per-role env files, written with LF endings and deleted right after the start).
# The model and Git are set up in the browser (⚙ Setup); .env is optional (advanced settings).
#
#   .\scripts\wslc-up.ps1            # download the latest image and start, opens the browser
#   .\scripts\wslc-up.ps1 -Build     # build the image from this folder instead (developers)
#   .\scripts\wslc-up.ps1 -Mock      # smoke test: also start the mock LLM from tests/ (no real model)
#   .\scripts\wslc-down.ps1          # stop and remove the containers (volumes are kept)
#
# Differences from Docker Compose: no restart policy (after a reboot run this script again), and
# the test GitLab from tests/docker-compose.test.yml is not started (it needs ~4 GB RAM).
param([switch]$Build, [switch]$Rebuild, [switch]$Mock, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$NET = "devteam"
$ROLES = "lead", "architect", "backend", "frontend", "qa", "devops"

# ---------- .env (same file as for Docker Compose)
if (-not (Test-Path .env)) {
    # a fresh installation: everything else is set up in the browser
    [IO.File]::WriteAllText((Join-Path $root ".env"), "# AI DevTeam: advanced settings, see .env.example. The model and Git are set up in the browser.`n",
                            (New-Object Text.UTF8Encoding $false))
}
$cfg = @{}
foreach ($line in Get-Content .env -Encoding UTF8) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    $v = $v.Trim()
    if ($v.Length -ge 2 -and (($v[0] -eq "'" -and $v[-1] -eq "'") -or ($v[0] -eq '"' -and $v[-1] -eq '"'))) { $v = $v.Substring(1, $v.Length - 2) }
    $cfg[$k.Trim()] = $v
}
function Get-Cfg([string]$name, [string]$default = "") { if ($cfg.ContainsKey($name) -and $cfg[$name]) { $cfg[$name] } else { $default } }

if ($Mock) {
    # the mock LLM accepts any key; GIT_* stay as they are (publishing fails without a Git host, which is expected)
    $cfg["LLM_MODEL"] = "openai/mock"; $cfg["LLM_BASE_URL"] = "http://mockllm:9000/v1"
    foreach ($r in $ROLES) { if (-not (Get-Cfg "$($r.ToUpper())_API_KEY")) { $cfg["$($r.ToUpper())_API_KEY"] = "mock" } }
}

# INTERNAL_SECRET signs the requests between the containers. Generate it once and keep it in .env.
if ((Get-Cfg "INTERNAL_SECRET").Length -lt 32) {
    $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $lines = @(Get-Content .env -Encoding UTF8 | Where-Object { $_ -notmatch '^\s*INTERNAL_SECRET\s*=' }) + "INTERNAL_SECRET=$secret"
    [IO.File]::WriteAllText((Join-Path $root ".env"), ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding $false))
    $cfg["INTERNAL_SECRET"] = $secret
    Write-Host "INTERNAL_SECRET was empty: generated and saved to .env"
}

$bind = Get-Cfg "UI_BIND" "127.0.0.1"
if ($bind -ne "127.0.0.1" -and -not ((Get-Cfg "UI_USER") -and (Get-Cfg "UI_PASSWORD"))) {
    throw "UI_BIND=$bind without UI_USER/UI_PASSWORD would let anyone on the network run commands through the agents"
}

$Build = $Build -or $Rebuild
$IMAGE = if ($Build) { "devteam-agent:latest" } else { Get-Cfg "DEVTEAM_IMAGE" "ghcr.io/nikvladimirovich2/ai-devteam:latest" }

$common = "INTERNAL_SECRET", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_CONTEXT_WINDOW", "LLM_TIMEOUT",
          "LLM_SSL_VERIFY_DISABLE", "LLM_FUNCTION_CALLING", "AGENT_MAX_ITER", "SHELL_TIMEOUT"
$extra = @{
    lead   = "MAX_QA_ROUNDS", "MAX_CI_ROUNDS", "GIT_PROVIDER", "GIT_URL", "UI_USER", "UI_PASSWORD"
    devops = "GIT_PROVIDER", "GIT_URL", "GIT_TOKEN", "GIT_NAMESPACE", "GIT_DEFAULT_BRANCH", "GIT_VISIBILITY",
             "GIT_SSL_VERIFY_DISABLE", "CI_WAIT_TIMEOUT", "CI_POLL_INTERVAL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"
}

function Invoke-Wslc { $ErrorActionPreference = "Continue"; & wslc.exe @args; if ($LASTEXITCODE -ne 0) { throw "wslc $($args -join ' ') failed ($LASTEXITCODE)" } }
# PowerShell 5.1 turns a native command's stderr into an error under "Stop": probe with "Continue"
function Test-Wslc { $ErrorActionPreference = "Continue"; & wslc.exe @args *> $null; return $LASTEXITCODE -eq 0 }

# ---------- image, network, volumes
if ($Build) {
    Write-Host "Building $IMAGE (first time ~5-10 min)..."
    Invoke-Wslc build -t $IMAGE ./agent
} else {
    Write-Host "Downloading $IMAGE (first time ~1.7 GB)..."
    if (-not (Test-Wslc pull $IMAGE)) {
        if (Test-Wslc image inspect $IMAGE) { Write-Host "No connection to the registry: using the image already downloaded." }
        elseif (Test-Path ./agent/Dockerfile) { Write-Host "Download failed: building the image here instead."; $IMAGE = "devteam-agent:latest"; Invoke-Wslc build -t $IMAGE ./agent }
        else { throw "Could not download $IMAGE. Check the internet connection and run the script again." }
    }
}
if (-not (Test-Wslc network inspect $NET)) { Invoke-Wslc network create $NET | Out-Null }
foreach ($v in "devteam_workspace", "devteam_leaddata", "devteam_devopsdata") { if (-not (Test-Wslc volume inspect $v)) { Invoke-Wslc volume create $v | Out-Null } }
$certs = (Join-Path $root (Get-Cfg "CERTS_DIR" "agent/certs")) -replace '\\', '/'
if (-not (Test-Path $certs)) { New-Item -ItemType Directory -Force $certs | Out-Null }

# ---------- containers
$names = @($ROLES | ForEach-Object { "devteam-$_" }) + "devteam-mockllm"
foreach ($n in $names) { if (Test-Wslc container inspect $n) { Test-Wslc remove -f $n | Out-Null } }

if ($Mock) {
    $tests = (Join-Path $root "tests") -replace '\\', '/'
    Invoke-Wslc run -d --name devteam-mockllm --network $NET --network-alias mockllm -v "${tests}:/tests:ro" `
        --health-cmd "curl -fs http://localhost:9000/v1/models" `
        $IMAGE python -m uvicorn mock_llm:app --host 0.0.0.0 --port 9000 --app-dir /tests | Out-Null
}

foreach ($r in $ROLES) {
    $P = $r.ToUpper()
    $vars = [ordered]@{ ROLE = $r; LEAD_URL = "http://lead:8000"; WORKSPACE = "/workspace" }
    foreach ($k in $common + @("${P}_MODEL", "${P}_API_KEY") + @($extra[$r])) { if ($k) { $vars[$k] = Get-Cfg $k } }
    if ($r -eq "lead") { $vars["DB_PATH"] = "/data/devteam.db" }
    $envFile = Join-Path $env:TEMP "devteam-$r-$([guid]::NewGuid().ToString('N')).env"
    try {
        $text = ($vars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`n"
        [IO.File]::WriteAllText($envFile, $text + "`n", (New-Object Text.UTF8Encoding $false))   # LF, no BOM
        $runArgs = @("run", "-d", "--name", "devteam-$r", "--network", $NET, "--network-alias", $r,
                  "-v", "devteam_workspace:/workspace", "-v", "${certs}:/certs:ro", "--env-file", $envFile)
        if ($r -eq "lead") { $runArgs += @("-v", "devteam_leaddata:/data", "-p", "${bind}:$(Get-Cfg 'UI_PORT' '8080'):8000") }
        if ($r -eq "devops") { $runArgs += @("-v", "devteam_devopsdata:/devops-data") }
        Invoke-Wslc @runArgs $IMAGE | Out-Null
    } finally { Remove-Item $envFile -Force -ErrorAction SilentlyContinue }
    Write-Host "started devteam-$r"
}
$url = "http://localhost:$(Get-Cfg 'UI_PORT' '8080')"
Write-Host "Waiting for the team..."
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "$url/health" | Out-Null; break } catch { Start-Sleep 2 }
}
Write-Host "Ready: $url   (logs: wslc logs -f devteam-lead)"
if (-not $NoBrowser) { Start-Process $url }
