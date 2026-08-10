$ErrorActionPreference = 'Stop'
$Version = '0.10.0'
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$InstallRoot = Join-Path $env:LOCALAPPDATA 'ServiceSlate'
$DataRoot = Join-Path $env:LOCALAPPDATA 'ServiceSlateData'
$ToolsRoot = Join-Path $InstallRoot '.tools'
$UvExe = Join-Path $ToolsRoot 'uv.exe'

function Step([string]$Text) { Write-Host "`n> $Text" -ForegroundColor Cyan }
function Copy-App {
    New-Item -ItemType Directory -Force -Path $InstallRoot, $DataRoot, $ToolsRoot | Out-Null
    $items = @('src','docs','windows\ServiceSlate.ico','pyproject.toml','run_serviceslate.py','configure_office_network.py','README.md')
    foreach ($item in $items) {
        $from = Join-Path $SourceRoot $item
        if (Test-Path $from) {
            $to = Join-Path $InstallRoot $item
            $parent = Split-Path -Parent $to
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            if (Test-Path $to) { Remove-Item -Recurse -Force $to }
            Copy-Item -Recurse -Force $from $to
        }
    }
}

Step 'Copying ServiceSlate'
Copy-App

if (-not (Test-Path $UvExe)) {
    Step 'Getting the small ServiceSlate setup helper'
    $zip = Join-Path $env:TEMP 'serviceslate-uv.zip'
    $extract = Join-Path $env:TEMP 'serviceslate-uv'
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $found = Get-ChildItem $extract -Filter uv.exe -Recurse | Select-Object -First 1
    if (-not $found) { throw 'The setup helper could not be unpacked.' }
    Copy-Item $found.FullName $UvExe -Force
}

Step 'Preparing ServiceSlate (first setup can use the internet once)'
Push-Location $InstallRoot
try {
    & $UvExe python install 3.13
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the included Python runtime.' }
    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        & $UvExe venv --python 3.13 .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the ServiceSlate runtime.' }
    }
    & $UvExe pip install --python '.venv\Scripts\python.exe' --upgrade .
    if ($LASTEXITCODE -ne 0) { throw 'Could not install ServiceSlate components.' }
    # Classic Outlook support is optional. Failure here must not block ServiceSlate.
    try { & $UvExe pip install --python '.venv\Scripts\python.exe' 'pywin32>=306' | Out-Null } catch {}
} finally { Pop-Location }

$Launcher = Join-Path $InstallRoot 'Start-ServiceSlate.vbs'
$escapedRoot = $InstallRoot.Replace('"','""')
$escapedData = $DataRoot.Replace('"','""')
@"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$escapedRoot"
shell.Environment("PROCESS")("SERVICESLATE_DATA_DIR") = "$escapedData"
shell.Run Chr(34) & "$escapedRoot\.venv\Scripts\pythonw.exe" & Chr(34) & " " & Chr(34) & "$escapedRoot\run_serviceslate.py" & Chr(34), 0, False
"@ | Set-Content -Encoding ASCII $Launcher

$OfficeConfigLauncher = Join-Path $InstallRoot 'Configure-ServiceSlateOffice.cmd'
@"
@echo off
set "SERVICESLATE_DATA_DIR=$escapedData"
cd /d "$escapedRoot"
"$escapedRoot\.venv\Scripts\python.exe" "$escapedRoot\configure_office_network.py"
echo.
pause
"@ | Set-Content -Encoding ASCII $OfficeConfigLauncher

Step 'Adding shortcuts'
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$start = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($shortcutPath in @((Join-Path $desktop 'ServiceSlate.lnk'), (Join-Path $start 'ServiceSlate.lnk'))) {
    $sc = $ws.CreateShortcut($shortcutPath)
    $sc.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $sc.Arguments = '"' + $Launcher + '"'
    $sc.WorkingDirectory = $InstallRoot
    $sc.Description = 'Open ServiceSlate'
    $sc.IconLocation = (Join-Path $InstallRoot 'windows\ServiceSlate.ico') + ',0'
    $sc.Save()
}
$officeShortcut = $ws.CreateShortcut((Join-Path $start 'ServiceSlate - Configure Office Network.lnk'))
$officeShortcut.TargetPath = $OfficeConfigLauncher
$officeShortcut.WorkingDirectory = $InstallRoot
$officeShortcut.Description = 'Configure ServiceSlate Office Host or Join Office mode'
$officeShortcut.IconLocation = (Join-Path $InstallRoot 'windows\ServiceSlate.ico') + ',0'
$officeShortcut.Save()

@"
ServiceSlate $Version
Installed: $(Get-Date -Format o)
Program: $InstallRoot
Data: $DataRoot
"@ | Set-Content -Encoding UTF8 (Join-Path $InstallRoot 'INSTALLATION.txt')

Step 'Opening ServiceSlate'
Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ('"' + $Launcher + '"')
Write-Host "`nDone. Use the ServiceSlate shortcut from now on." -ForegroundColor Green
Write-Host "Your company data stays in: $DataRoot"
