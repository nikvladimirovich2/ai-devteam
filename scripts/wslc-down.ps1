# Stops and removes the AI DevTeam containers started by wslc-up.ps1. Volumes (task history,
# project folders, saved settings) are kept; add -Volumes to delete them too.
# wslc keeps separate containers for a normal and for an administrator PowerShell («sessions»):
# the team is stopped in every session this PowerShell can open.
param([switch]$Volumes)
$ErrorActionPreference = "Continue"
$me = $env:USERNAME
$sessions = @(& wslc.exe system session list 2>$null | ForEach-Object { if ($_ -match '(wslc-cli\S*)') { $Matches[1] } })
foreach ($n in "wslc-cli-$me", "wslc-cli-admin-$me") { if ($sessions -notcontains $n) { $sessions += $n } }
$stopped = 0
foreach ($s in ($sessions | Select-Object -Unique)) {
    $sa = @("--session", $s)
    $list = & wslc.exe @sa list -a 2>$null
    if ($LASTEXITCODE -ne 0) { continue }        # cannot open this session (e.g. the admin one)
    if (-not ($list | Select-String "devteam-")) { continue }
    foreach ($n in "lead", "architect", "backend", "frontend", "qa", "devops", "mockllm") { & wslc.exe @sa remove -f "devteam-$n" *> $null }
    if ($Volumes) { foreach ($v in "devteam_workspace", "devteam_leaddata", "devteam_devopsdata") { & wslc.exe @sa volume remove $v *> $null } }
    & wslc.exe @sa network remove devteam *> $null
    Write-Host "stopped ($s)"
    $stopped++
}
if ($stopped -eq 0) { Write-Host "AI DevTeam is not running." }
