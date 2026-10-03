$ErrorActionPreference = "Stop"

$dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$valueName = "Docker Desktop"

if (-not (Test-Path -LiteralPath $dockerDesktop)) {
    throw "Docker Desktop executable was not found at $dockerDesktop"
}

$startupEntry = @{
    Path = $runKey
    Name = $valueName
    Value = ('"{0}"' -f $dockerDesktop)
    PropertyType = "String"
    Force = $true
}
New-ItemProperty @startupEntry | Out-Null

$configured = (Get-ItemProperty -Path $runKey -Name $valueName).$valueName
Write-Output "Docker Desktop user-login startup configured: $configured"
