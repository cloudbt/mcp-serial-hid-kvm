<#
.SYNOPSIS
Example 6 - generic PowerShell caller for the serial-hid-kvm TCP JSON Lines API.

Call any API method without Python and without the MCP server. One JSON object
per line over TCP; response is {id, ok, result|error}.

.EXAMPLE
# Ping
.\Invoke-Kvm.ps1 -Method ping

.EXAMPLE
# Type a command into the focused target shell ({enter} tag supported)
.\Invoke-Kvm.ps1 -Method type_text -Params @{ text = 'Get-Date{enter}' }

.EXAMPLE
# Send Ctrl+Alt+Del-style combos (here: Win+L to lock)
.\Invoke-Kvm.ps1 -Method send_key -Params @{ key = 'l'; modifiers = @('win') }

.EXAMPLE
# Screenshot straight to a file (second target on port 9331)
.\Invoke-Kvm.ps1 -Port 9331 -Method capture_frame -Params @{ quality = 90 } -OutFile "$env:USERPROFILE\Documents\kvm-evidence\shot.jpg"

.EXAMPLE
# Device / serial / capture status as objects
(.\Invoke-Kvm.ps1 -Method get_device_info).serial
#>
[CmdletBinding()]
param(
    [string]$KvmHost = '127.0.0.1',
    [int]$Port = 9329,
    [Parameter(Mandatory)][string]$Method,
    [hashtable]$Params = @{},
    [string]$OutFile,          # for capture_frame: decode jpeg_b64 to this path
    [int]$TimeoutMs = 30000
)

$client = [System.Net.Sockets.TcpClient]::new()
try {
    $client.ReceiveTimeout = $TimeoutMs
    $client.SendTimeout = $TimeoutMs
    $client.Connect($KvmHost, $Port)
    $stream = $client.GetStream()
    $writer = [System.IO.StreamWriter]::new($stream, [System.Text.UTF8Encoding]::new($false))
    $reader = [System.IO.StreamReader]::new($stream, [System.Text.UTF8Encoding]::new($false))

    $request = @{
        id     = [guid]::NewGuid().ToString('N').Substring(0, 8)
        method = $Method
        params = $Params
    } | ConvertTo-Json -Compress -Depth 6

    $writer.WriteLine($request)
    $writer.Flush()

    $line = $reader.ReadLine()
    if (-not $line) { throw "KVM server closed the connection" }
    $response = $line | ConvertFrom-Json
    if (-not $response.ok) { throw "KVM error: $($response.error)" }

    $result = $response.result
    if ($OutFile -and $result.jpeg_b64) {
        $dir = Split-Path -Parent $OutFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        [IO.File]::WriteAllBytes($OutFile, [Convert]::FromBase64String($result.jpeg_b64))
        [pscustomobject]@{ saved = $OutFile; width = $result.width; height = $result.height }
    }
    else {
        $result
    }
}
finally {
    $client.Dispose()
}
