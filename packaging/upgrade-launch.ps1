param([ValidateSet('Wait','Launch','Restore','Retarget')][string]$Phase)
$ErrorActionPreference = 'Stop'
# The native installer sets these values. No path is interpolated into shell code.
$installRoot = $env:DT_UPGRADE_ROOT
$targetExe = $env:DT_UPGRADE_EXE
$expectedSource = $env:DT_UPGRADE_SOURCE
$previousExe = $env:DT_UPGRADE_PREVIOUS
$logPath = Join-Path $installRoot 'upgrade.log'
function Write-UpgradeLog([string]$message) {
    if (Test-Path -LiteralPath (Join-Path $installRoot '.typeless-install')) {
        Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' ' + $message) -Encoding UTF8
    }
}
function Start-Application([string]$exe, [string]$receipt) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $exe
    $start.WorkingDirectory = Split-Path -LiteralPath $exe
    $start.UseShellExecute = $false
    # Both old and new PyInstaller bootloaders must create a fresh runtime.
    foreach ($key in @($start.EnvironmentVariables.Keys)) {
        if ($key -like '_PYI*' -or $key -eq '_MEIPASS2' -or $key -like 'DT_UPGRADE_*') {
            $start.EnvironmentVariables.Remove($key)
        }
    }
    $start.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    $start.EnvironmentVariables['DT_UPDATE_READY_FILE'] = $receipt
    if ($env:DT_UPGRADE_MINIMIZED -eq '1') { $start.Arguments = '--minimized' }
    return [System.Diagnostics.Process]::Start($start)
}
$child = $null
$readyPath = $null
try {
    if ($Phase -eq 'Retarget') {
        # An in-app update from an unpacked onedir keeps its old shortcut usable.
        # Only the explicitly handed-off EXE is replaced; siblings/data stay put.
        $installedPrefix = [IO.Path]::GetFullPath($installRoot).TrimEnd('\') + '\versions\'
        if ($env:DT_UPGRADE_HANDOFF -and $previousExe -and
            -not [IO.Path]::GetFullPath($previousExe).StartsWith($installedPrefix,[StringComparison]::OrdinalIgnoreCase)) {
            $temporaryEntry = $previousExe + '.upgrade-' + [Guid]::NewGuid().ToString('N')
            $backupEntry = $previousExe + '.before-upgrade-' + [Guid]::NewGuid().ToString('N')
            try {
                [IO.File]::Copy($env:DT_UPGRADE_PACKAGE, $temporaryEntry, $false)
                [IO.File]::Replace($temporaryEntry, $previousExe, $backupEntry)
                Write-UpgradeLog 'Portable entry now forwards to the installed version; original EXE backed up'
            } finally {
                if (Test-Path -LiteralPath $temporaryEntry) { Remove-Item -LiteralPath $temporaryEntry }
            }
        }
        exit 0
    }
    if ($Phase -eq 'Restore') {
        if ($previousExe -and (Test-Path -LiteralPath $previousExe -PathType Leaf)) {
            Start-Application $previousExe '' | Out-Null
            Write-UpgradeLog 'Extraction failed; previous application restart requested'
        }
        exit 0
    }
    if ($Phase -eq 'Wait') {
        $waitPid = 0
        if ($env:DT_UPGRADE_WAIT_PID -and
            (-not [int]::TryParse($env:DT_UPGRADE_WAIT_PID, [ref]$waitPid) -or $waitPid -lt 0)) {
            throw 'Invalid wait process'
        }
        if ($waitPid -gt 0) {
            $previous = Get-Process -Id $waitPid -ErrorAction SilentlyContinue
            if ($env:DT_UPGRADE_HANDOFF) {
                if (-not $previous) { exit 4 }
                $acceptPath = [IO.Path]::ChangeExtension($env:DT_UPGRADE_HANDOFF, '.accept')
                $cancelPath = [IO.Path]::ChangeExtension($env:DT_UPGRADE_HANDOFF, '.cancel')
                if (Test-Path -LiteralPath $cancelPath) { exit 4 }
                [IO.File]::WriteAllText($env:DT_UPGRADE_HANDOFF, [string]$waitPid)
                $approvalDeadline = [DateTime]::UtcNow.AddSeconds(20)
                $accepted = $false
                do {
                    if (Test-Path -LiteralPath $cancelPath) { exit 4 }
                    if (Test-Path -LiteralPath $acceptPath) {
                        if ([IO.File]::ReadAllText($acceptPath).Trim() -eq [string]$waitPid) {
                            $accepted = $true; break
                        }
                    }
                    if ($previous.HasExited) { exit 4 }
                    Start-Sleep -Milliseconds 100
                } while ([DateTime]::UtcNow -lt $approvalDeadline)
                if (-not $accepted) { exit 4 }
            }
            if ($previous) {
                $exitDeadline = [DateTime]::UtcNow.AddSeconds(60)
                while (-not $previous.WaitForExit(100)) {
                    if ($env:DT_UPGRADE_HANDOFF -and (Test-Path -LiteralPath $cancelPath)) { exit 4 }
                    if ([DateTime]::UtcNow -ge $exitDeadline) {
                        throw 'The previous application has not exited; no files were replaced'
                    }
                }
                if ($env:DT_UPGRADE_HANDOFF -and (Test-Path -LiteralPath $cancelPath)) { exit 4 }
            }
        }
        exit 0
    }
    if ($expectedSource -notmatch '^[0-9a-f]{40}$') { throw 'Missing build identity' }
    $fullRoot = [IO.Path]::GetFullPath($installRoot).TrimEnd('\') + '\versions\'
    $fullExe = [IO.Path]::GetFullPath($targetExe)
    if (-not $fullExe.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Application is outside the installation'
    }
    if (-not (Test-Path -LiteralPath $fullExe -PathType Leaf)) { throw 'Application is missing' }
    $readyPath = Join-Path $installRoot ('.update-ready-' + [Guid]::NewGuid().ToString('N'))
    Write-UpgradeLog 'Starting new application; awaiting application readiness'
    $child = Start-Application $fullExe $readyPath
    # First start of a new version backs up user data before reporting readiness.
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        if (Test-Path -LiteralPath $readyPath) {
            if (([IO.File]::ReadAllText($readyPath)).Trim() -eq $expectedSource) {
                Write-UpgradeLog 'Application ready; upgrade succeeded'
                exit 0
            }
            throw 'Application returned a different build identity'
        }
        if ($child.HasExited) { throw ('Application exited before readiness: ' + $child.ExitCode) }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Application readiness timed out'
} catch {
    Write-UpgradeLog ('Upgrade failed: ' + $_.Exception.Message)
    # Only the unready process created above is eligible for cleanup.
    if ($child -and -not $child.HasExited) {
        $child.Kill(); $child.WaitForExit(5000) | Out-Null
    }
    if ($Phase -eq 'Launch' -and $previousExe -and $previousExe -ne $targetExe -and
        (Test-Path -LiteralPath $previousExe -PathType Leaf)) {
        try {
            Start-Application $previousExe '' | Out-Null
            Write-UpgradeLog 'Previous application restart requested; existing registration retained'
        } catch { Write-UpgradeLog 'Previous application could not be restarted; program files retained' }
    }
    Write-Output $_.Exception.Message
    exit 2
} finally {
    if ($readyPath -and (Test-Path -LiteralPath $readyPath)) {
        Remove-Item -LiteralPath $readyPath -Force
    }
    if ($child) { $child.Dispose() }
}
