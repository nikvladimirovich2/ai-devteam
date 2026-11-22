# Removes AI DevTeam from Windows (WSL containers). In the installation folder:
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1             # asks before deleting data
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -KeepData   # keep task history and projects
#   powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Yes        # no questions, delete everything
# Removes: the containers, the network, the image, the desktop shortcuts and the installation folder;
# the volumes with the task history, projects and saved settings unless -KeepData. Repositories already
# published to GitHub/GitLab are NOT touched. When no other WSL containers or images are left, the
# disk of the WSL container VM is deleted too, to give the space back.
param([switch]$KeepData, [switch]$Yes)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot

function W { & wslc.exe @args *> $null; return $LASTEXITCODE -eq 0 }

if (-not (Get-Command wslc.exe -ErrorAction SilentlyContinue)) { Write-Host "wslc is not installed: nothing to stop." }
else {
    Write-Host "Stopping and removing the containers..."
    foreach ($n in "lead", "architect", "backend", "frontend", "qa", "devops", "mockllm") { W remove -f "devteam-$n" | Out-Null }
    W network remove devteam | Out-Null

    $dropData = -not $KeepData
    if ($dropData -and -not $Yes) {
        $a = Read-Host "Delete the task history, the project folders and the saved keys/tokens too? [y/N]"
        $dropData = $a -match '^(y|yes)$'
    }
    if ($dropData) {
        foreach ($v in "devteam_workspace", "devteam_leaddata", "devteam_devopsdata") { W volume remove $v | Out-Null }
        Write-Host "Data volumes deleted."
    } else { Write-Host "Data volumes kept (devteam_workspace, devteam_leaddata, devteam_devopsdata)." }

    Write-Host "Removing the image..."
    $images = & wslc.exe image list 2>$null | Select-String "ai-devteam|devteam-agent" | ForEach-Object { ($_ -split '\s+')[0] + ":" + ($_ -split '\s+')[1] }
    foreach ($i in $images) { W rmi $i | Out-Null }

    # Give the disk space back when nothing else uses WSL containers
    $leftC = @(& wslc.exe list -a 2>$null | Select-Object -Skip 1 | Where-Object { $_.Trim() })
    $leftI = @(& wslc.exe image list 2>$null | Select-Object -Skip 1 | Where-Object { $_.Trim() })
    if ($dropData -and $leftC.Count -eq 0 -and $leftI.Count -eq 0) {
        & wslc.exe system session terminate *> $null
        Start-Sleep 3
        Get-ChildItem (Join-Path $env:LOCALAPPDATA "wslc\sessions") -Recurse -Include storage.vhdx, swap.vhdx -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
        Write-Host "WSL container disk deleted (it is created again when needed)."
    } elseif ($leftC.Count -or $leftI.Count) {
        Write-Host "Other WSL containers/images exist: the WSL container disk is kept."
    }
}

$desktop = [Environment]::GetFolderPath("Desktop")
foreach ($s in "AI DevTeam.lnk", "AI DevTeam - stop.lnk") { Remove-Item (Join-Path $desktop $s) -Force -ErrorAction SilentlyContinue }

if (Test-Path (Join-Path $root "agent\Dockerfile")) {
    Write-Host "This is a source checkout: the folder $root is kept."
} elseif (Test-Path (Join-Path $root "scripts\wslc-up.ps1")) {
    Set-Location $HOME
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Folder $root deleted."
}
Write-Host "AI DevTeam is removed. Repositories in GitHub/GitLab were not touched."
