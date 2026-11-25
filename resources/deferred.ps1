param([Parameter(Mandatory=$true)][string]$Receipt)
$ErrorActionPreference = 'Stop'
$Receipt = [IO.Path]::GetFullPath($Receipt)
try { Set-Location -LiteralPath ([IO.Path]::GetTempPath()) } catch {}
Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' -or $_.Name -like '_MEIPASS*' -or $_.Name -in @('PYTHONHOME','PYTHONPATH','TCL_LIBRARY','TK_LIBRARY','BNGEARTH_PARENT') } | ForEach-Object { Remove-Item -LiteralPath "Env:\$($_.Name)" -ErrorAction SilentlyContinue }

function Write-JsonAtomic($destination, $json) {
    if (-not $destination) { return }
    $dir = [IO.Path]::GetDirectoryName($destination)
    if ($dir -and -not [IO.Directory]::Exists($dir)) {
        [IO.Directory]::CreateDirectory($dir) | Out-Null
    }
    $temporary = $destination + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($destination)) {
        $previous = $temporary + '.previous'
        [IO.File]::Replace($temporary, $destination, $previous)
        Remove-Item -LiteralPath $previous -ErrorAction SilentlyContinue
    } else {
        [IO.File]::Move($temporary, $destination)
    }
}

function Save-Record($value) {
    $json = $value | ConvertTo-Json -Depth 10
    if ($value.sidecar) {
        Write-JsonAtomic $value.sidecar $json
    }
    if ($Receipt) {
        $rDir = [IO.Path]::GetDirectoryName($Receipt)
        if (-not $rDir -or [IO.Directory]::Exists($rDir)) {
            Write-JsonAtomic $Receipt $json
        }
    }
}

function Hash-File($path) {
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TargetHash($path) {
    $resolved = [IO.Path]::GetFullPath($path)
    $bytes = [Text.Encoding]::UTF8.GetBytes($resolved)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($bytes)
        return ([BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()).Substring(0, 12)
    } finally {
        $sha.Dispose()
    }
}

function Acquire-SwapLock($lockPath, $timeoutSec = 90, $staleSec = 120) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $fs = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
                $payload = (@{ pid = $PID; ts = $ts } | ConvertTo-Json -Compress)
                $raw = [Text.Encoding]::UTF8.GetBytes($payload)
                $fs.Write($raw, 0, $raw.Length)
            } finally {
                $fs.Close()
            }
            return $true
        } catch [IO.IOException] {
            try {
                if ([IO.File]::Exists($lockPath)) {
                    $nowTs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
                    $lockInfo = Get-Content -LiteralPath $lockPath -Raw -ErrorAction Stop | ConvertFrom-Json
                    $holderPid = 0
                    if ($null -ne $lockInfo.pid) { $holderPid = [int]$lockInfo.pid }
                    $lockTs = 0.0
                    if ($null -ne $lockInfo.ts) { $lockTs = [double]$lockInfo.ts }
                    $isStale = ($lockTs -gt 0 -and ($nowTs - $lockTs) -gt $staleSec)
                    if (-not $isStale -and $holderPid -gt 0 -and ($nowTs - $lockTs) -gt 1.0) {
                        if ($null -eq (Get-Process -Id $holderPid -ErrorAction SilentlyContinue)) {
                            $isStale = $true
                        }
                    }
                    if ($isStale) {
                        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
                        continue
                    }
                }
            } catch {
            }
            Start-Sleep -Milliseconds 100
        }
    }
    throw 'Timed out waiting for cross-addon swap lock.'
}

function Release-SwapLock($lockPath) {
    if (-not $lockPath -or -not [IO.File]::Exists($lockPath)) { return }
    try {
        $lockInfo = Get-Content -LiteralPath $lockPath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ([int]$lockInfo.pid -eq $PID) {
            Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}

function Cleanup-Staged($rec) {
    if ($rec.mode -in @('source', 'onedir', 'host')) {
        if ($rec.stageDir -and [IO.Directory]::Exists($rec.stageDir)) {
            Remove-Item -LiteralPath $rec.stageDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } elseif ($rec.pending -and [IO.File]::Exists($rec.pending)) {
        if ((Hash-File $rec.pending) -eq $rec.replacementSha) {
            Remove-Item -LiteralPath $rec.pending -ErrorAction SilentlyContinue
        }
    }
}

function Validate-PendingPath($tgt, $pend) {
    if ([IO.Path]::GetDirectoryName($tgt) -ne [IO.Path]::GetDirectoryName($pend) -or
        -not [IO.Path]::GetFileName($pend).StartsWith([IO.Path]::GetFileName($tgt) + '.earthBridgeRailFixes.') -or
        -not $pend.EndsWith('.pending')) {
        throw 'Invalid staged-file path.'
    }
}

function Test-DirItemsMatch($tgt, $items) {
    foreach ($item in $items) {
        $dest = Join-Path $tgt ($item.rel -replace '/', '\')
        if ($item.expectedSha) {
            if (-not [IO.File]::Exists($dest) -or (Hash-File $dest) -ne $item.expectedSha) {
                return $false
            }
        } elseif ([IO.File]::Exists($dest)) {
            return $false
        }
    }
    return $true
}

function Invoke-RestageOnCurrent($rec, $tgt, $receiptFile) {
    Cleanup-Staged $rec
    $restagePy = Join-Path $PSScriptRoot 'restage_on_current.py'
    if (-not [IO.File]::Exists($restagePy)) {
        throw 'Missing cooperative restage helper (restage_on_current.py).'
    }
    Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' -or $_.Name -like '_MEIPASS*' -or $_.Name -in @('PYTHONHOME','PYTHONPATH','TCL_LIBRARY','TK_LIBRARY') } | ForEach-Object { Remove-Item -LiteralPath "Env:\$($_.Name)" -ErrorAction SilentlyContinue }
    $prevNoHold = $env:BNE_COMPANION_NO_HOLD
    $prevDontWrite = $env:PYTHONDONTWRITEBYTECODE
    $prevParent = $env:BNGEARTH_PARENT
    $env:BNE_COMPANION_NO_HOLD = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:BNGEARTH_PARENT = '0'
    try {
        $ran = $false
        $exitCode = 1
        if ($env:BNE_RESTAGE_PYTHON -and [IO.File]::Exists($env:BNE_RESTAGE_PYTHON)) {
            $proc = Start-Process -FilePath $env:BNE_RESTAGE_PYTHON -ArgumentList @('-B', $restagePy, '--receipt', $receiptFile) -WindowStyle Hidden -Wait -PassThru
            $exitCode = $proc.ExitCode
            if ($exitCode -eq 0 -or $exitCode -eq 2) {
                $ran = $true
            }
        } elseif ($rec.mode -eq 'exe' -and $tgt.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($tgt)) {
            for ($tryIdx = 0; $tryIdx -lt 15; $tryIdx++) {
                try {
                    $proc = Start-Process -FilePath $tgt -ArgumentList @('--run-addon', $restagePy, '--receipt', $receiptFile) -WindowStyle Hidden -Wait -PassThru
                    $exitCode = $proc.ExitCode
                    if ($exitCode -eq 0 -or $exitCode -eq 2) {
                        $ran = $true
                        break
                    }
                } catch {
                }
                Start-Sleep -Seconds 1
            }
        }
        if (-not $ran) {
            foreach ($cand in @(
                @{ exe = 'py'; prefix = @('-3.12', '-B') },
                @{ exe = 'py'; prefix = @('-3', '-B') },
                @{ exe = 'python'; prefix = @('-B') },
                @{ exe = 'python3'; prefix = @('-B') }
            )) {
                $pyCmd = Get-Command $cand.exe -ErrorAction SilentlyContinue
                if ($pyCmd) {
                    try {
                        $argList = @($cand.prefix) + @($restagePy, '--receipt', $receiptFile)
                        $proc = Start-Process -FilePath $pyCmd.Source -ArgumentList $argList -WindowStyle Hidden -Wait -PassThru
                        $exitCode = $proc.ExitCode
                        if ($exitCode -eq 0 -or $exitCode -eq 2) {
                            $ran = $true
                            break
                        }
                    } catch {
                    }
                }
            }
            if (-not $ran) {
                throw 'Could not start BeamNG.Earth --run-addon or Python to restage cooperative patch.'
            }
        }
        return $exitCode
    } finally {
        if ($null -eq $prevNoHold) { Remove-Item Env:\BNE_COMPANION_NO_HOLD -ErrorAction SilentlyContinue } else { $env:BNE_COMPANION_NO_HOLD = $prevNoHold }
        if ($null -eq $prevDontWrite) { Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue } else { $env:PYTHONDONTWRITEBYTECODE = $prevDontWrite }
        if ($null -eq $prevParent) { Remove-Item Env:\BNGEARTH_PARENT -ErrorAction SilentlyContinue } else { $env:BNGEARTH_PARENT = $prevParent }
    }
}

function Mod-Active($configuration) {
    if (-not $configuration.enabled -or -not $configuration.modPath -or -not [IO.File]::Exists($configuration.modPath)) {
        return $false
    }
    return $true
}

function Show-Popup($message, $title, $icon = 'Information') {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($message, $title, 'OK', $icon) | Out-Null
    } catch {
    }
}

function Show-ReadyNotice($rec) {
    if ($rec.notify -and -not $rec.skipReadyNotice) {
        Show-Popup 'Bridge, Rail & Tunnel Fixes are staged and ready. Close BeamNG.Earth completely, wait a few seconds, then launch BeamNG.Earth normally.' 'Bridge, Rail & Tunnel Fixes - restart required'
    }
}

$swapLockPath = $null
try {
    $Receipt = [IO.Path]::GetFullPath($Receipt)
    $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath($record.target)
    if ($record.mode -notin @('source', 'onedir', 'host')) {
        $pending = [IO.Path]::GetFullPath($record.pending)
        Validate-PendingPath $target $pending
    }

    if ($record.companionPid) {
        $companionEnd = (Get-Date).AddSeconds(30)
        while ($null -ne (Get-Process -Id $record.companionPid -ErrorAction SilentlyContinue)) {
            if ((Get-Date) -gt $companionEnd) {
                throw 'Setup did not finish closing. Please try again.'
            }
            Start-Sleep -Milliseconds 200
        }
    }

    Show-ReadyNotice $record

    $deadline = (Get-Date).AddHours(12)
    $ready = $false
    do {
        if (-not [IO.File]::Exists($Receipt) -or -not [IO.File]::Exists($record.config)) {
            Cleanup-Staged $record
            return
        }
        $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
        $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
        if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
            Cleanup-Staged $record
            return
        }
        if ($record.mode -notin @('source', 'onedir', 'host') -and $target.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
            $busy = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'BeamNG.Earth%.exe'" |
                Where-Object { $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -eq $target }).Count -gt 0
        } else {
            $busy = $record.serverPid -gt 0 -and $null -ne (Get-Process -Id $record.serverPid -ErrorAction SilentlyContinue)
        }
        if (-not $busy) { $ready = $true; break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    if (-not $ready) {
        throw 'Timed out waiting for BeamNG.Earth to close. Please try again.'
    }

    $targetHash = Get-TargetHash $target
    $swapLockPath = Join-Path ([IO.Path]::GetTempPath()) ("bne_companion_swap_" + $targetHash + ".lock")
    Acquire-SwapLock $swapLockPath | Out-Null

    if (-not [IO.File]::Exists($Receipt) -or -not [IO.File]::Exists($record.config)) {
        Cleanup-Staged $record
        return
    }
    $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
    if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
        Cleanup-Staged $record
        return
    }

    if ($record.operation -eq 'install' -and -not $record.standalone -and -not (Mod-Active $configuration)) {
        Cleanup-Staged $record
        $record.state = 'cancelled'
        $record.message = 'The add-on was disabled or removed; patch cancelled.'
        Save-Record $record
        return
    }

    if ($record.mode -in @('source', 'onedir', 'host')) {
        if (-not (Test-DirItemsMatch $target $record.items)) {
            $restageRc = Invoke-RestageOnCurrent $record $target $Receipt
            if ($restageRc -eq 2) { return }
            if ($restageRc -ne 0) { throw 'Server files changed after preparation; cooperative restage failed.' }
            $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
            if ($record.state -in @('installed', 'restored', 'cancelled')) {
                Save-Record $record
                return
            }
        }
        foreach ($item in $record.items) {
            $dest = Join-Path $target ($item.rel -replace '/', '\')
            if ($item.expectedSha) {
                if (-not [IO.File]::Exists($dest) -or (Hash-File $dest) -ne $item.expectedSha) {
                    throw "File changed before apply: $($item.rel)"
                }
            } elseif ([IO.File]::Exists($dest)) {
                throw "File unexpectedly appeared before apply: $($item.rel)"
            }
        }
        foreach ($item in $record.items) {
            $dest = Join-Path $target ($item.rel -replace '/', '\')
            if ($item.action -eq 'delete') {
                if ([IO.File]::Exists($dest)) { Remove-Item -LiteralPath $dest -Force }
            } else {
                if ((Hash-File $item.staged) -ne $item.replacementSha) {
                    throw "Staged file checksum mismatch: $($item.rel)"
                }
                $destDir = [IO.Path]::GetDirectoryName($dest)
                if ($destDir -and -not [IO.Directory]::Exists($destDir)) {
                    [IO.Directory]::CreateDirectory($destDir) | Out-Null
                }
                Copy-Item -LiteralPath $item.staged -Destination $dest -Force
                if ((Hash-File $dest) -ne $item.replacementSha) {
                    throw "Post-copy checksum mismatch: $($item.rel)"
                }
            }
        }
        Cleanup-Staged $record
    } else {
        if ((Hash-File $target) -ne $record.expectedSha) {
            $restageRc = Invoke-RestageOnCurrent $record $target $Receipt
            if ($restageRc -eq 2) { return }
            if ($restageRc -ne 0) { throw 'Server changed after preparation; an update was preserved.' }
            $record = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
            if ($record.state -in @('installed', 'restored', 'cancelled')) {
                Save-Record $record
                return
            }
            $pending = [IO.Path]::GetFullPath($record.pending)
            Validate-PendingPath $target $pending
        }
        if ((Hash-File $target) -ne $record.expectedSha) { throw 'Server changed after preparation; an update was preserved.' }
        if ((Hash-File $pending) -ne $record.replacementSha) { throw 'Staged file changed; original server was preserved.' }
        $replaced = $false
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            $latest = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
            $configuration = Get-Content -LiteralPath $record.config -Raw | ConvertFrom-Json
            if ($latest.token -ne $record.token -or $configuration.requestToken -ne $record.token) {
                Cleanup-Staged $record
                return
            }
            if ((Hash-File $target) -ne $record.expectedSha) {
                throw 'Server changed before replacement; original file preserved.'
            }
            try {
                $swapBackup = $target + '.earthBridgeRailFixes.swap.' + [guid]::NewGuid().ToString('N')
                [IO.File]::Replace($pending, $target, $swapBackup)
                $replaced = $true
                break
            } catch [IO.IOException] {
                Start-Sleep -Seconds 2
            }
        }
        if (-not $replaced) { throw 'Windows kept the executable locked. Close BeamNG.Earth and try again.' }
        if ((Hash-File $target) -ne $record.replacementSha) { throw 'Replacement verification failed; original backup available.' }
        Remove-Item -LiteralPath $swapBackup -ErrorAction SilentlyContinue
    }

    if ($record.operation -eq 'restore') {
        $record.state = 'restored'
        $record.message = 'Original BeamNG.Earth restored. Start BeamNG.Earth normally.'
    } else {
        $record.state = 'installed'
        $record.message = 'Bridge, Rail & Tunnel fixes installed. Start BeamNG.Earth normally.'
    }
    Save-Record $record

    if ($record.operation -eq 'restore' -and $record.cleanupOnRestore) {
        if ($record.backup -and [IO.File]::Exists($record.backup)) {
            Remove-Item -LiteralPath $record.backup -Force -ErrorAction SilentlyContinue
        }
        if ($record.sidecar -and [IO.File]::Exists($record.sidecar)) {
            Remove-Item -LiteralPath $record.sidecar -Force -ErrorAction SilentlyContinue
        }
        if ($record.installReceipt -and [IO.File]::Exists($record.installReceipt)) {
            Remove-Item -LiteralPath $record.installReceipt -Force -ErrorAction SilentlyContinue
        }
        if ($record.lastTargetFile -and [IO.File]::Exists($record.lastTargetFile)) {
            Remove-Item -LiteralPath $record.lastTargetFile -Force -ErrorAction SilentlyContinue
        }
    }

    if ($record.relaunchAfterSwap -and $target.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($target)) {
        Start-Process -FilePath $target -WorkingDirectory ([IO.Path]::GetDirectoryName($target)) | Out-Null
    }

    if ($record.completionNotice) {
        Show-Popup $record.completionNotice 'Bridge, Rail & Tunnel Fixes'
    }
} catch {
    if ($record) {
        $record.state = 'error'
        $record.message = $_.Exception.Message
        Save-Record $record
        if ($record.notify -and $record.skipReadyNotice) {
            Show-Popup $_.Exception.Message 'Bridge, Rail & Tunnel Fixes - setup error' 'Error'
        }
    }
} finally {
    if ($swapLockPath) {
        Release-SwapLock $swapLockPath
    }
}
