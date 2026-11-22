# Stops and removes the AI DevTeam containers started by wslc-up.ps1. Volumes (task history and
# project folders) are kept; add -Volumes to delete them too.
param([switch]$Volumes)
$ErrorActionPreference = "Continue"
foreach ($n in "lead", "architect", "backend", "frontend", "qa", "devops", "mockllm") {
    & wslc.exe remove -f "devteam-$n" *> $null
}
if ($Volumes) { foreach ($v in "devteam_workspace", "devteam_leaddata", "devteam_devopsdata") { & wslc.exe volume remove $v *> $null } }
& wslc.exe network remove devteam *> $null
Write-Host "stopped"
