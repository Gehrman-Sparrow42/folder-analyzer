$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Use the active interpreter; callers may invoke this from any Python environment.
    python -m PyInstaller --noconfirm --clean --onefile --console --noupx `
        --name FolderAnalyzer --add-data 'web:web' server.py
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }

    $artifact = Join-Path $PSScriptRoot 'dist\FolderAnalyzer.exe'
    $checksum = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(
        (Join-Path $PSScriptRoot 'dist\SHA256SUMS.txt'),
        "$checksum  FolderAnalyzer.exe`n",
        [Text.UTF8Encoding]::new($false)
    )
    Write-Host "Built $artifact"
}
finally {
    Pop-Location
}
