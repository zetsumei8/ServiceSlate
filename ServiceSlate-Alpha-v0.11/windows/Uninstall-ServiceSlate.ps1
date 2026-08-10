$ErrorActionPreference='Stop'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'ServiceSlate'
$DataRoot = Join-Path $env:LOCALAPPDATA 'ServiceSlateData'
$desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ServiceSlate.lnk'
$start = Join-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs') 'ServiceSlate.lnk'
Remove-Item $desktop,$start -Force -ErrorAction SilentlyContinue
Remove-Item $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'ServiceSlate program files were removed.'
Write-Host "Company data was intentionally kept at: $DataRoot"
Write-Host 'Delete that folder separately only if you truly want to erase the company data.'
