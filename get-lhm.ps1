# get-lhm.ps1 — fetches LibreHardwareMonitorLib.dll and HidSharp.dll into .\lib
#
#   powershell -ExecutionPolicy Bypass -File .\get-lhm.ps1
#
# Pulls straight from nuget.org, picks the .NET Framework build (which is what
# pythonnet loads by default), and clears the mark-of-the-web block that stops
# .NET from loading downloaded assemblies.

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # Invoke-WebRequest is glacial without this

function Get-NuGetDll {
    param(
        [string]   $Id,
        [string]   $FileName,
        [string[]] $Prefer
    )

    $idLower = $Id.ToLower()
    Write-Host "`n== $Id" -ForegroundColor Cyan

    $index   = Invoke-RestMethod "https://api.nuget.org/v3-flatcontainer/$idLower/index.json"
    $version = $index.versions | Where-Object { $_ -notmatch '-' } | Select-Object -Last 1
    if (-not $version) { throw "No stable release found for $Id." }
    Write-Host "   version   $version"

    $url  = "https://api.nuget.org/v3-flatcontainer/$idLower/$version/$idLower.$version.nupkg"
    $zip  = Join-Path $env:TEMP "$idLower.$version.zip"
    $dest = Join-Path $env:TEMP "$idLower-$version"

    Invoke-WebRequest -Uri $url -OutFile $zip
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $dest -Force

    foreach ($tfm in $Prefer) {
        $candidate = Join-Path $dest "lib\$tfm\$FileName"
        if (Test-Path $candidate) {
            Copy-Item $candidate -Destination ".\lib\$FileName" -Force
            $size = (Get-Item ".\lib\$FileName").Length
            Write-Host "   target    $tfm"
            Write-Host "   installed lib\$FileName ($('{0:N0}' -f $size) bytes)" -ForegroundColor Green
            return
        }
    }

    Write-Host "   None of the preferred targets exist. This package ships:" -ForegroundColor Yellow
    Get-ChildItem (Join-Path $dest "lib") -Directory | ForEach-Object { Write-Host "     $($_.Name)" }
    throw "No suitable build of $FileName."
}

if (-not (Test-Path ".\main.py")) {
    throw "Run this from the folder that contains main.py."
}

New-Item -ItemType Directory -Force -Path ".\lib" | Out-Null

Get-NuGetDll -Id "LibreHardwareMonitorLib" -FileName "LibreHardwareMonitorLib.dll" `
             -Prefer @("net472", "net48", "netstandard2.0", "net8.0")

Get-NuGetDll -Id "HidSharp" -FileName "HidSharp.dll" `
             -Prefer @("net472", "net46", "net45", "netstandard2.0")

Get-ChildItem ".\lib\*.dll" | Unblock-File
Write-Host "`nUnblocked. Contents of lib\:" -ForegroundColor Cyan
Get-ChildItem ".\lib" | Format-Table Name, Length -AutoSize

Write-Host "Next: python diagnose.py  (from an administrator terminal)" -ForegroundColor Cyan
