#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Initialize', 'Install', 'Update', 'Resume', 'Status', 'Stop', 'Start', 'Rollback')]
    [string]$Action,
    [string]$Root = (Join-Path $env:ProgramData 'SilverTestPlatform'),
    [string]$Package,
    [string]$WinSWPath,
    [string]$Python = 'python.exe',
    [switch]$Collab,
    [string]$Backup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root)
$Roles = @('web', 'worker', 'collab')
$StatePath = Join-Path $Root 'active.json'
$PendingPath = Join-Path $Root 'pending.json'
$DataPath = Join-Path $Root 'data'
$BackupRoot = Join-Path $Root 'backups'
$ServiceDir = Join-Path $Root 'services'
$ConfigPath = Join-Path $Root 'config\.env'

function Invoke-Native([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path -Leaf $Program) exited with code $LASTEXITCODE." }
}

function Read-Json([string]$Path) {
    if ([IO.File]::Exists($Path)) { Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
}

function Write-Json([string]$Path, $Value) {
    $temp = "$Path.tmp"
    [IO.File]::WriteAllText($temp, (ConvertTo-Json -Depth 5 -InputObject $Value))
    if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temp, $Path) }
}

function Service-Name([string]$Role) { "SilverTestPlatform-$Role" }

function Find-Service([string]$Role) {
    Get-Service -Name (Service-Name $Role) -ErrorAction SilentlyContinue
}

function Release-Path($Release) {
    if ($Release.directory -notmatch '^(v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?|preview-[0-9a-f]{12})-[0-9a-f]{12}$') {
        throw 'Invalid installed release directory.'
    }
    Join-Path $Root "releases\$($Release.directory)"
}

function Invoke-Runtime($Release, [string[]]$Arguments) {
    $directory = Release-Path $Release
    $interpreter = Join-Path $directory '.venv\Scripts\python.exe'
    $runtime = Join-Path $directory 'deployment\runtime.py'
    Invoke-Native $interpreter (@('-X', 'utf8', $runtime, $Root) + $Arguments)
}

function Stage-Release([bool]$WithCollab) {
    if (!$Package) { throw 'Supply -Package with a release ZIP and its adjacent .sha256 file.' }
    $zip = (Resolve-Path -LiteralPath $Package).ProviderPath
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $expected = ([IO.File]::ReadAllText("$zip.sha256").Trim() -split '\s+')[0]
    if ($hash -ne $expected) { throw 'Release ZIP SHA256 does not match its checksum file.' }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $reader = New-Object IO.StreamReader($archive.GetEntry('release.json').Open())
        try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
        if ($manifest.release -notmatch '^(v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?|preview-[0-9a-f]{12})$') {
            throw 'Invalid release ID in package.'
        }
    } finally { $archive.Dispose() }

    $release = [ordered]@{
        release = $manifest.release
        directory = "$($manifest.release)-$($hash.Substring(0, 12))"
        checksum = $hash
        collab = $WithCollab
    }
    $directory = Release-Path $release
    $verified = Join-Path $directory '.package.sha256'
    if ([IO.File]::Exists($verified)) {
        if ([IO.File]::ReadAllText($verified) -ne $hash) { throw 'Existing package hash differs.' }
        return $release
    }
    $basePython = (Get-Command $Python -CommandType Application).Source
    $versionCheck = "import sys, sysconfig; sys.exit(not (sysconfig.get_platform() == 'win-amd64' and sys.implementation.name == 'cpython' and sys.version_info[:3] == (3, 10, 18)))"
    Invoke-Native $basePython @('-c', $versionCheck)
    if ([IO.Directory]::Exists($directory)) {
        $incomplete = "$directory.incomplete-$([guid]::NewGuid().ToString('N'))"
        [IO.Directory]::Move($directory, $incomplete)
    }
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $directory)
    Invoke-Native $basePython @('-m', 'venv', (Join-Path $directory '.venv'))
    $interpreter = Join-Path $directory '.venv\Scripts\python.exe'
    Invoke-Native $interpreter @('-m', 'pip', '--isolated', 'install', '--no-index', '--disable-pip-version-check',
        '--find-links', (Join-Path $directory 'wheels'), '-r', (Join-Path $directory 'requirements.txt'))
    Invoke-Runtime $release @('preflight')
    [IO.File]::WriteAllText($verified, $hash)
    return $release
}

function Copy-Data([string]$Source, [string]$Destination) {
    & robocopy.exe $Source $Destination /E /COPY:DAT /DCOPY:DAT /XJ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE." }
}

function Stop-Services($Current, [switch]$CheckIdle) {
    # Reboot during the maintenance window must not restart an old worker.
    foreach ($role in $Roles) {
        $service = Find-Service $role
        if ($service) { Set-Service -Name $service.Name -StartupType Disabled }
    }
    foreach ($role in $Roles) {
        if ($CheckIdle -and $role -eq 'worker') { Invoke-Runtime $Current @('idle') }
        $service = Find-Service $role
        if ($service -and $service.Status -ne 'Stopped') {
            Stop-Service -Name $service.Name
            $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(90))
        }
    }
}

function Set-ServiceConfig($Release) {
    $directory = Release-Path $Release
    foreach ($role in $Roles) {
        if ($role -eq 'collab' -and !$Release.collab) { continue }
        $id = Service-Name $role
        $wrapper = Join-Path $ServiceDir "$id.exe"
        if (![IO.File]::Exists($wrapper)) {
            Copy-Item -LiteralPath (Join-Path $ServiceDir 'WinSW.exe') -Destination $wrapper
        }
        $executable = [Security.SecurityElement]::Escape((Join-Path $directory '.venv\Scripts\python.exe'))
        $arguments = [Security.SecurityElement]::Escape(('-X utf8 "{0}" "{1}" {2}' -f
            (Join-Path $directory 'deployment\runtime.py'), $Root, $role))
        $working = [Security.SecurityElement]::Escape($directory)
        $logs = [Security.SecurityElement]::Escape((Join-Path $Root 'logs'))
        $xml = @"
<service>
  <id>$id</id>
  <name>Silver Test Platform ($role)</name>
  <description>Runs the Silver Test Platform $role process.</description>
  <executable>$executable</executable>
  <arguments>$arguments</arguments>
  <workingdirectory>$working</workingdirectory>
  <serviceaccount><domain>NT AUTHORITY</domain><user>LocalService</user></serviceaccount>
  <startmode>Manual</startmode>
  <stoptimeout>75sec</stoptimeout>
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <onfailure action="restart" delay="10sec"/>
  <logpath>$logs</logpath>
  <log mode="roll"/>
</service>
"@
        [IO.File]::WriteAllText((Join-Path $ServiceDir "$id.xml"), $xml)
        if (!(Find-Service $role)) { Invoke-Native $wrapper @('install') }
    }
}

function Start-Release($Release) {
    $enabled = @('worker')
    if ($Release.collab) { $enabled += 'collab' }
    $enabled += 'web'
    foreach ($role in $enabled) {
        $id = Service-Name $role
        Set-Service -Name $id -StartupType Manual
        Start-Service -Name $id
    }
    $health = @('health')
    if ($Release.collab) { $health += '--collab' }
    Invoke-Runtime $Release $health
    foreach ($role in $enabled) {
        if ((Find-Service $role).Status -ne 'Running') { throw "The $role service exited during startup." }
    }
}

function Enable-Autostart($Release) {
    foreach ($role in $Roles) {
        if ($role -ne 'collab' -or $Release.collab) {
            Set-Service -Name (Service-Name $role) -StartupType Automatic
        }
    }
}

function Complete-Deployment($Pending) {
    $candidate = $Pending.candidate
    try {
        if ($Pending.bootstrap) {
            Invoke-Runtime $candidate @('bootstrap')
            $Pending.bootstrap = $false
            Write-Json $PendingPath $Pending
        }
        Set-ServiceConfig $candidate
        Start-Release $candidate
        Write-Json $StatePath $candidate
    } catch {
        $failure = $_
        try { Stop-Services $candidate }
        catch { Write-Warning "Could not stop all services: $_" }
        throw $failure
    }
    Remove-Item -LiteralPath $PendingPath
    Enable-Autostart $candidate
    Write-Host "Active release $($candidate.release). Logs: $(Join-Path $Root 'logs')"
}

if ($Action -eq 'Status') {
    $state = Read-Json $StatePath
    $pending = Read-Json $PendingPath
    if ($state) { Write-Host "Recorded active release: $($state.release)" }
    if ($pending) { Write-Host "Incomplete deployment: $($pending.candidate.release). Recovery snapshot: $($pending.backup)" }
    foreach ($role in $Roles) { Find-Service $role }
    return
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run deployment commands in an elevated Windows PowerShell session.'
}

$marker = Join-Path $Root '.silver-deployment'
if ($Action -eq 'Initialize') {
    if ([IO.Directory]::Exists($Root) -and [IO.Directory]::GetFileSystemEntries($Root).Length -gt 0) {
        throw 'Initialize requires a dedicated empty deployment directory.'
    }
    if (!$WinSWPath) { throw 'Supply -WinSWPath with the WinSW 2.x executable.' }
    $wrapper = (Resolve-Path -LiteralPath $WinSWPath).ProviderPath
    $example = Join-Path (Split-Path -Parent $PSScriptRoot) '.env.example'
    [IO.Directory]::CreateDirectory($Root) | Out-Null
    Invoke-Native 'icacls.exe' @($Root, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F',
        '*S-1-5-18:(OI)(CI)F', '*S-1-5-19:(OI)(CI)RX')
    foreach ($directory in @($DataPath, $ServiceDir, $BackupRoot, (Join-Path $Root 'config'), (Join-Path $Root 'logs'))) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    foreach ($directory in @($DataPath, (Join-Path $Root 'logs'))) {
        Invoke-Native 'icacls.exe' @($directory, '/grant:r', '*S-1-5-19:(OI)(CI)M')
    }
    Invoke-Native 'icacls.exe' @($BackupRoot, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F')
    Copy-Item -LiteralPath $wrapper -Destination (Join-Path $ServiceDir 'WinSW.exe')
    Copy-Item -LiteralPath $example -Destination $ConfigPath
    [IO.File]::WriteAllText($marker, 'Silver Test Platform Windows deployment')
    Write-Host "Configure $ConfigPath before running Install."
    return
}

if (![IO.File]::Exists($marker)) { throw 'Initialize this deployment directory first.' }
$lock = [IO.File]::Open((Join-Path $Root 'deployment.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    $state = Read-Json $StatePath
    $pending = Read-Json $PendingPath
    if ($pending -and $Action -notin @('Resume', 'Rollback')) {
        throw "Deployment is incomplete. Use Resume or Rollback. Snapshot: $($pending.backup)"
    }
    if ($Action -eq 'Resume') {
        if (!$pending) { throw 'No incomplete deployment to resume.' }
        if ($pending.phase -eq 'restore') { throw 'The restore is incomplete. Retry Rollback with a fresh empty database.' }
        Stop-Services $pending.candidate
        Complete-Deployment $pending
        return
    }
    if ($Action -eq 'Rollback') {
        if ($Backup -notmatch '^\d{8}-\d{6}-[0-9a-f]{8}$') { throw 'Supply a completed backup directory name from backups.' }
        foreach ($role in $Roles) {
            $service = Find-Service $role
            if ($service -and $service.Status -ne 'Stopped') { throw 'Stop all services before rollback.' }
        }
        $snapshot = Join-Path $BackupRoot $Backup
        $previous = Read-Json (Join-Path $snapshot 'release.json')
        if (!$previous) { throw "No completed snapshot named $Backup." }
        Invoke-Runtime $previous @('empty-db')
        Stop-Services $previous
        $pending = @{ candidate = $previous; backup = $Backup; bootstrap = $false; phase = 'restore' }
        Write-Json $PendingPath $pending
        $id = [guid]::NewGuid().ToString('N')
        $restored = Join-Path $Root "restored-data-$id"
        Copy-Data (Join-Path $snapshot 'data') $restored
        Invoke-Runtime $previous @('restore', (Join-Path $snapshot 'database.dump'))
        $preserved = Join-Path $Root "preserved-data-$id"
        if ([IO.Directory]::Exists($DataPath)) { [IO.Directory]::Move($DataPath, $preserved) }
        try { [IO.Directory]::Move($restored, $DataPath) }
        catch {
            if ([IO.Directory]::Exists($preserved)) { [IO.Directory]::Move($preserved, $DataPath) }
            throw
        }
        Invoke-Native 'icacls.exe' @($DataPath, '/grant:r', '*S-1-5-19:(OI)(CI)M')
        $pending.phase = 'activate'
        Write-Json $PendingPath $pending
        Complete-Deployment $pending
        Write-Host "Preserved pre-rollback files at $preserved."
        return
    }
    if ($Action -eq 'Install' -and $state) { throw 'An active release already exists. Use Update.' }
    if ($Action -ne 'Install' -and !$state) { throw 'There is no active release. Use Install.' }
    if ($Action -eq 'Start') {
        Start-Release $state
        Enable-Autostart $state
        return
    }

    $wasRunning = $false
    $backupName = $null
    if ($Action -in @('Install', 'Update')) {
        $withCollab = $Collab.IsPresent
        if ($state) { $withCollab = $state.collab }
        $candidate = Stage-Release $withCollab
        Invoke-Runtime $candidate @('preflight')
        if ($state -and $candidate.checksum -eq $state.checksum) { throw 'This package is already active.' }
    }
    if ($state) {
        $wasRunning = @($Roles | ForEach-Object { Find-Service $_ } | Where-Object Status -eq Running).Count -gt 0
        Invoke-Runtime $state @('idle')
        try {
            Stop-Services $state -CheckIdle
            if ($Action -eq 'Stop') { return }
            $backupName = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
            $snapshot = Join-Path $BackupRoot $backupName
            [IO.Directory]::CreateDirectory($snapshot) | Out-Null
            Invoke-Runtime $state @('backup', $snapshot)
            Copy-Data $DataPath (Join-Path $snapshot 'data')
            Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $snapshot '.env')
            Write-Json (Join-Path $snapshot 'release.json') $state
            Write-Host "Rollback snapshot: $snapshot"
        } catch {
            if ($wasRunning) {
                Start-Release $state
                Enable-Autostart $state
            }
            throw
        }
    }
    $pending = @{ candidate = $candidate; backup = $backupName; bootstrap = $true; phase = 'activate' }
    Write-Json $PendingPath $pending
    Complete-Deployment $pending
} finally { $lock.Dispose() }
