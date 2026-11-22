# AI DevTeam installer for Windows (WSL containers, no Docker Desktop). In PowerShell:
#   irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 | iex
# It downloads the prebuilt image, starts the team, opens http://localhost:<port> and puts an
# "AI DevTeam" shortcut on the desktop (start it again after a Windows restart).
# The AI model and GitHub/GitLab are then set up in the browser. Run it again to update.
# Options (environment): DEVTEAM_DIR (default %USERPROFILE%\ai-devteam), DEVTEAM_REF (branch or tag).
& {
    $ErrorActionPreference = "Stop"
    $repo = "nikvladimirovich2/ai-devteam"
    $ref = if ($env:DEVTEAM_REF) { $env:DEVTEAM_REF } else { "main" }
    $dir = if ($env:DEVTEAM_DIR) { $env:DEVTEAM_DIR } else { Join-Path $HOME "ai-devteam" }

    # 1. WSL containers (wslc ships with WSL 3.0.1+)
    if (-not (Get-Command wslc.exe -ErrorAction SilentlyContinue)) {
        if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
            Write-Host "Updating WSL to get WSL containers..."
            & wsl.exe --update
        }
        if (-not (Get-Command wslc.exe -ErrorAction SilentlyContinue)) {
            Write-Host ""
            Write-Host "WSL containers are not available yet. Do this once:" -ForegroundColor Yellow
            Write-Host "  1. Open PowerShell as administrator and run:  wsl --install --no-distribution"
            Write-Host "  2. Restart Windows."
            Write-Host "  3. Run this installer again."
            Write-Host "(Virtualization must be enabled in the BIOS. Details: DEPLOY.md, section 11.)"
            return
        }
    }

    # 2. files
    Write-Host "Installing AI DevTeam into $dir"
    foreach ($d in "scripts", "certs") { New-Item -ItemType Directory -Force (Join-Path $dir $d) | Out-Null }
    foreach ($f in "scripts/wslc-up.ps1", "scripts/wslc-down.ps1", "scripts/uninstall.ps1", ".env.example") {
        Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/$repo/$ref/$f" -OutFile (Join-Path $dir $f)
    }
    $envFile = Join-Path $dir ".env"
    if (-not (Test-Path $envFile)) {
        $port = 8080
        while ($true) {
            $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
            try { $l.Start(); $l.Stop(); break } catch { $port++ }
        }
        $text = "# AI DevTeam. The model and Git are set up in the browser; advanced options: .env.example`nUI_PORT=$port`nCERTS_DIR=certs`n"
        [IO.File]::WriteAllText($envFile, $text, (New-Object Text.UTF8Encoding $false))
    }

    # 3. desktop shortcuts: start the team (and open the browser) / stop it, with one double click
    $up = Join-Path $dir "scripts\wslc-up.ps1"
    $down = Join-Path $dir "scripts\wslc-down.ps1"
    try {
        $ws = New-Object -ComObject WScript.Shell
        $desktop = [Environment]::GetFolderPath("Desktop")
        foreach ($s in @(@("AI DevTeam.lnk", $up, ""), @("AI DevTeam - stop.lnk", $down, "-NoExit "))) {
            $sh = $ws.CreateShortcut((Join-Path $desktop $s[0]))
            $sh.TargetPath = "powershell.exe"
            $sh.Arguments = "$($s[2])-NoProfile -ExecutionPolicy Bypass -File `"$($s[1])`""
            $sh.WorkingDirectory = $dir
            $sh.Save()
        }
    } catch { Write-Host "(no desktop shortcut: $($_.Exception.Message))" }

    # 4. start
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $up
    Write-Host ""
    Write-Host "Next: follow Setup in the browser (AI model key, GitHub/GitLab token)."
    Write-Host "After a Windows restart: double-click 'AI DevTeam' on the desktop. Stop: 'AI DevTeam - stop'."
    Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File `"$dir\scripts\uninstall.ps1`""
}
