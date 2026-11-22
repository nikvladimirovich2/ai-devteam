# Removes AI DevTeam from Windows (WSL containers). In the installation folder:
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1             # asks before deleting data
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -KeepData   # keep task history and projects
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Yes        # no questions, delete everything
# Removes: the containers, the network, the images, the desktop shortcuts and the installation folder;
# the volumes with the task history, projects and saved settings unless -KeepData. Repositories already
# published to GitHub/GitLab are NOT touched.
# wslc keeps separate containers for a normal and for an administrator PowerShell («sessions»): this
# script cleans every session it can open. A session that has nothing else left gets its disk deleted,
# which gives the space back. -Session <name> limits it to one session.
param([switch]$KeepData, [switch]$Yes, [string]$Session = "")
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot

function W { & wslc.exe @args *> $null; return $LASTEXITCODE -eq 0 }
function Out-Wslc { $o = & wslc.exe @args 2>$null; if ($LASTEXITCODE -eq 0) { $o } }

function Get-Sessions {
    if ($Session) { return @($Session) }
    $names = @(Out-Wslc system session list | ForEach-Object { if ($_ -match '(wslc-cli\S*)') { $Matches[1] } })
    $me = $env:USERNAME
    foreach ($n in "wslc-cli-$me", "wslc-cli-admin-$me") { if ($names -notcontains $n) { $names += $n } }
    return $names | Select-Object -Unique
}

if (-not (Get-Command wslc.exe -ErrorAction SilentlyContinue)) { Write-Host "wslc is not installed: nothing to remove." }
else {
    $dropData = -not $KeepData
    if ($dropData -and -not $Yes) {
        $a = Read-Host "Delete the task history, the project folders and the saved keys/tokens too? [y/N]"
        $dropData = $a -match '^(y|yes)$'
    }
    foreach ($s in Get-Sessions) {
        $sa = @("--session", $s)
        $all = Out-Wslc @sa list -a
        if ($null -eq $all) { continue }          # this session cannot be opened (e.g. the admin one)
        Write-Host "[$s] stopping and removing the containers..."
        foreach ($n in "lead", "architect", "backend", "frontend", "qa", "devops", "mockllm") { W @sa remove -f "devteam-$n" | Out-Null }
        W @sa network remove devteam | Out-Null
        if ($dropData) { foreach ($v in "devteam_workspace", "devteam_leaddata", "devteam_devopsdata") { W @sa volume remove $v | Out-Null } }
        # images by ID: also the untagged (<none>) ones left by updates
        $ids = @(Out-Wslc @sa image list | Where-Object { $_ -match 'ai-devteam|devteam-agent' } | ForEach-Object { ($_ -split '\s+')[2] } | Select-Object -Unique)
        foreach ($i in $ids) { W @sa rmi -f $i | Out-Null }
        Write-Host "[$s] removed: containers, $($ids.Count) image(s)$(if ($dropData) { ', data volumes' } else { ' (data volumes kept)' })"
        $leftC = @(Out-Wslc @sa list -a | Select-Object -Skip 1 | Where-Object { $_.Trim() })
        $leftI = @(Out-Wslc @sa image list | Select-Object -Skip 1 | Where-Object { $_.Trim() })
        $leftV = @(Out-Wslc @sa volume list | Select-Object -Skip 1 | Where-Object { $_.Trim() })
        if ($leftC.Count -eq 0 -and $leftI.Count -eq 0 -and $leftV.Count -eq 0) {
            & wslc.exe @sa system session terminate *> $null
            $dir = Join-Path $env:LOCALAPPDATA "wslc\sessions\$s"
            # the VM releases its disk a few seconds after the session stops
            for ($i = 0; $i -lt 15; $i++) {
                Start-Sleep 2
                foreach ($f in "storage.vhdx", "swap.vhdx") { Remove-Item (Join-Path $dir $f) -Force -ErrorAction SilentlyContinue }
                if (-not (Test-Path (Join-Path $dir "storage.vhdx"))) { break }
            }
            if (Test-Path (Join-Path $dir "storage.vhdx")) { Write-Host "[$s] its disk is still in use: delete $dir\storage.vhdx later to get the space back." }
            else { Write-Host "[$s] nothing else uses it: its disk is deleted (created again when needed)." }
        } else {
            Write-Host "[$s] other containers, images or volumes exist: its disk is kept."
        }
    }
}

$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Session) { foreach ($sc in "AI DevTeam.lnk", "AI DevTeam - stop.lnk") { Remove-Item (Join-Path $desktop $sc) -Force -ErrorAction SilentlyContinue } }

if ($Session) {
    Write-Host "Only the session $Session was cleaned: the folder $root and the shortcuts are kept."
} elseif (Test-Path (Join-Path $root "agent\Dockerfile")) {
    Write-Host "This is a source checkout: the folder $root is kept."
} elseif (Test-Path (Join-Path $root "scripts\wslc-up.ps1")) {
    Set-Location $HOME
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Folder $root deleted."
}
Write-Host "AI DevTeam is removed. Repositories in GitHub/GitLab were not touched."
