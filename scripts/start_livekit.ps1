# Starts the local LiveKit server on this PC's current network address.
# The browser sends its audio to --node-ip (Chrome will not use 127.0.0.1),
# and that address changes whenever the laptop joins another Wi-Fi or hotspot,
# so it is looked up fresh on every start instead of typed in.
#
#   .\scripts\start_livekit.ps1
#
# Changed networks while it was running? Ctrl+C, run this again, and restart
# `sn-agent voice dev`.

$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object { $_.RouteMetric + $_.InterfaceMetric } |
    Select-Object -First 1
if (-not $route) {
    Write-Error 'No network connection found. Join a Wi-Fi network and try again.'
    exit 1
}
$ip = (Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1).IPAddress
if (-not $ip) {
    Write-Error "No IPv4 address on $($route.InterfaceAlias)."
    exit 1
}

Write-Host "LiveKit on $ip ($($route.InterfaceAlias))"
& (Join-Path $PSScriptRoot '..\livekit-server.exe') --dev --node-ip $ip
