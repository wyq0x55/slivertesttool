#Requires -Version 5.1
# Exercise the packaged deployment on a disposable GitHub-hosted Windows runner.
param([Parameter(Mandatory)][string]$Package, [Parameter(Mandatory)][string]$Python)

$ErrorActionPreference = 'Stop'
$env:Path = "$env:PGBIN;$env:Path"
$pg = Get-Service -Name 'postgresql-x64-*' | Select-Object -First 1
Set-Service -Name $pg.Name -StartupType Manual
Start-Service -Name $pg.Name
$env:PGPASSWORD = 'root' # GitHub Windows image's default test database password
foreach ($database in @('silver_deploy', 'silver_rollback')) {
    & createdb.exe -U postgres -h 127.0.0.1 $database
    if ($LASTEXITCODE -ne 0) { throw "Could not create $database." }
}

$root = Join-Path $env:ProgramData 'SilverDeploymentSmoke'
$tools = Join-Path $env:RUNNER_TEMP 'silver-setup'
Expand-Archive -LiteralPath $Package -DestinationPath $tools
$wrapper = Join-Path $tools 'WinSW.exe'
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe' -OutFile $wrapper
$manage = Join-Path $tools 'deployment\Manage-Silver.ps1'
$config = "DATABASE_URL=postgresql+psycopg2://postgres:root@127.0.0.1:5432/silver_deploy`n" +
    "SECRET_KEY=$([guid]::NewGuid().ToString('N'))`nRUNNER_BACKEND=mock`nHOST=127.0.0.1`nCOLLAB_HOST=127.0.0.1`n"

try {
    & $manage Initialize -Root $root -WinSWPath $wrapper
    [IO.File]::WriteAllText("$root\config\.env", $config)
    & $manage Install -Root $root -Package $Package -Python $Python -Collab
    $original = Get-Content -LiteralPath "$root\active.json" -Raw | ConvertFrom-Json
    [IO.File]::WriteAllText("$root\data\instance\smoke.txt", 'before update')

    # Two distinct packages exercise the version switch without building the wheels twice.
    $candidate = Join-Path $tools 'candidate.zip'
    Copy-Item -LiteralPath $Package -Destination $candidate
    $fixture = "import sys, zipfile; a = zipfile.ZipFile(sys.argv[1], 'a'); a.comment = b'upgrade smoke'; a.close()"
    & $Python -c $fixture $candidate
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the upgrade fixture.' }
    (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash | Set-Content -Encoding Ascii "$candidate.sha256"
    & $manage Update -Root $root -Package $candidate -Python $Python
    $updated = Get-Content -LiteralPath "$root\active.json" -Raw | ConvertFrom-Json
    if ($updated.checksum -eq $original.checksum) { throw 'Upgrade did not switch releases.' }
    [IO.File]::WriteAllText("$root\data\instance\smoke.txt", 'after update')
    & $manage Stop -Root $root
    $snapshots = @(Get-ChildItem -LiteralPath "$root\backups" -Directory)
    if ($snapshots.Count -ne 1) { throw 'Expected one pre-upgrade snapshot.' }
    [IO.File]::WriteAllText("$root\config\.env", $config.Replace('silver_deploy', 'silver_rollback'))
    & $manage Rollback -Root $root -Backup $snapshots[0].Name
    $restored = Get-Content -LiteralPath "$root\active.json" -Raw | ConvertFrom-Json
    if ($restored.checksum -ne $original.checksum -or
        [IO.File]::ReadAllText("$root\data\instance\smoke.txt") -ne 'before update') {
        throw 'Rollback did not restore the original release and data.'
    }
    & $manage Status -Root $root
} catch {
    Get-ChildItem -LiteralPath "$root\logs" -Filter '*.log' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host $_.Name
        Get-Content -LiteralPath $_.FullName -Tail 15
    }
    throw
} finally {
    Get-Service -Name 'SilverTestPlatform-*' -ErrorAction SilentlyContinue | Stop-Service -ErrorAction SilentlyContinue
}
