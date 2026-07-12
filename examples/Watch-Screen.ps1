<#
.SYNOPSIS
Example 7 - watch a long-running job screen and save evidence frames on change.

Polls capture_frame and saves a full-quality screenshot whenever the screen
changes noticeably (byte-size heuristic on a low-quality probe frame), plus a
final frame at the end. Designed for: terraform apply, JP1 job monitoring,
SM37 job screens, installers - anything you'd otherwise babysit.

The change detection is a lightweight heuristic (compressed-size delta), not a
pixel diff. It reliably catches "lots of new text / window changed" and stays
dependency-free; use the MCP wait_for_screen_change tool when you need a real
pixel diff.

.EXAMPLE
# Watch for 30 minutes, check every 20 s, save to the default evidence dir
.\Watch-Screen.ps1 -Label INC50968_JP1_JobRun -DurationMinutes 30

.EXAMPLE
# Second target (port 9331), more sensitive, faster polling
.\Watch-Screen.ps1 -Port 9331 -Label tfapply -DurationMinutes 10 -IntervalSeconds 10 -ChangePercent 3
#>
[CmdletBinding()]
param(
    [string]$KvmHost = '127.0.0.1',
    [int]$Port = 9329,
    [Parameter(Mandatory)][string]$Label,
    [double]$DurationMinutes = 30,
    [int]$IntervalSeconds = 20,
    [double]$ChangePercent = 5,     # probe-size delta (%) that counts as a change
    [string]$Dir = "$env:USERPROFILE\Documents\kvm-evidence"
)

$invokeKvm = Join-Path $PSScriptRoot 'Invoke-Kvm.ps1'
$safeLabel = ($Label -replace '[^A-Za-z0-9_\-]+', '_').Trim('_')
$outDir = Join-Path $Dir $safeLabel
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force $outDir | Out-Null }

function Get-ProbeSize {
    $r = & $invokeKvm -KvmHost $KvmHost -Port $Port -Method capture_frame -Params @{ quality = 30 }
    [Convert]::FromBase64String($r.jpeg_b64).Length
}

function Save-Frame([string]$Suffix) {
    $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
    $path = Join-Path $outDir "$ts`_$Suffix.jpg"
    & $invokeKvm -KvmHost $KvmHost -Port $Port -Method capture_frame -Params @{ quality = 90 } -OutFile $path | Out-Null
    Write-Host "saved: $path"
}

$deadline = (Get-Date).AddMinutes($DurationMinutes)
$lastSize = Get-ProbeSize
Save-Frame 'start'
Write-Host "watching $KvmHost`:$Port until $deadline (interval ${IntervalSeconds}s, threshold ${ChangePercent}%)"

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $IntervalSeconds
    try { $size = Get-ProbeSize } catch { Write-Warning $_; continue }
    $delta = if ($lastSize -gt 0) { [math]::Abs($size - $lastSize) * 100.0 / $lastSize } else { 100 }
    if ($delta -ge $ChangePercent) {
        Write-Host ("{0:HH:mm:ss} change {1:N1}%" -f (Get-Date), $delta)
        Save-Frame 'change'
        $lastSize = $size
    }
}

Save-Frame 'end'
Write-Host 'done.'
