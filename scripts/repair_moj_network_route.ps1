#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$destination = "163.29.130.137/32"
$interfaceIndex = 4
$nextHop = "192.168.68.1"

$routes = @(
    Get-NetRoute -PolicyStore PersistentStore `
        -DestinationPrefix $destination -ErrorAction SilentlyContinue |
        Where-Object {
            $_.InterfaceIndex -eq $interfaceIndex -and $_.NextHop -eq $nextHop
        }
)

if ($routes.Count -gt 1) {
    throw "Expected at most one MOJ route, found $($routes.Count)"
}
if ($routes.Count -eq 1) {
    $routes[0] | Remove-NetRoute -Confirm:$false
    Write-Host "Removed conflicting persistent route: $destination via $nextHop"
}
else {
    Write-Host "Conflicting persistent route is already absent."
}

Clear-DnsClientCache
Start-Sleep -Seconds 2

$remaining = @(
    Get-NetRoute -PolicyStore PersistentStore `
        -DestinationPrefix $destination -ErrorAction SilentlyContinue |
        Where-Object {
            $_.InterfaceIndex -eq $interfaceIndex -and $_.NextHop -eq $nextHop
        }
)
if ($remaining.Count -ne 0) {
    throw "The conflicting persistent route still exists."
}

$probe = Test-NetConnection "www.tpkonsale.moj.gov.tw" -Port 443 `
    -InformationLevel Detailed
if (-not $probe.TcpTestSucceeded) {
    throw "MOJ HTTPS is still unreachable after route repair."
}

Write-Host (
    "MOJ HTTPS reachable: {0}:443 via {1}" -f `
        $probe.RemoteAddress, $probe.InterfaceAlias
)
