[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$NoRestart,
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [ValidateRange(1, 65535)]
    [int]$AgentPort = 9001
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$appDirectory = Join-Path $projectRoot "social_demotest"
$agentDirectory = Join-Path $projectRoot "matchmaker_agent"
$pythonPath = Join-Path $projectRoot ".project-venv\Scripts\python.exe"
$healthUrl = "http://127.0.0.1:$Port/"
$agentHealthUrl = "http://127.0.0.1:$AgentPort/docs"
$netstatPath = Join-Path $env:SystemRoot "System32\netstat.exe"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $appDirectory "main.py") -PathType Leaf)) {
    throw "FastAPI entry point was not found: $appDirectory\main.py"
}
if (-not (Test-Path -LiteralPath (Join-Path $agentDirectory "agent_api.py") -PathType Leaf)) {
    throw "Matchmaker entry point was not found: $agentDirectory\agent_api.py"
}

function Get-ListeningProcessIds {
    param([int]$TargetPort)
    $processIds = @()
    $pattern = "^\s*TCP\s+\S+:$TargetPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($line in (& $netstatPath -ano -p tcp)) {
        if ($line -match $pattern) {
            $processIds += [int]$Matches[1]
        }
    }
    return @($processIds | Sort-Object -Unique)
}

function Test-AyueHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "<title>AI .*DEMO</title>"
    }
    catch {
        return $false
    }
}

function Test-MatchmakerHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $agentHealthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Start-HiddenPython {
    param(
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $pythonPath
    $startInfo.Arguments = $Arguments -join " "
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    return [System.Diagnostics.Process]::Start($startInfo)
}

$agentListeningProcessIds = @(Get-ListeningProcessIds -TargetPort $AgentPort)
if ($agentListeningProcessIds.Count -gt 0 -and -not (Test-MatchmakerHealth)) {
    throw "Port $AgentPort is owned by another application (PID $($agentListeningProcessIds -join ', ')). It was not stopped."
}
if ($agentListeningProcessIds.Count -gt 0 -and -not $NoRestart) {
    foreach ($processId in $agentListeningProcessIds) {
        Write-Host "Stopping the old Ayue matchmaker process (PID $processId)..."
        Stop-Process -Id $processId -Force
        Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $agentListeningProcessIds = @()
}

$listeningProcessIds = @(Get-ListeningProcessIds -TargetPort $Port)
if ($listeningProcessIds.Count -gt 0) {
    if (-not (Test-AyueHealth)) {
        throw "Port $Port is owned by another application (PID $($listeningProcessIds -join ', ')). It was not stopped."
    }
    if ($NoRestart) {
        if ($agentListeningProcessIds.Count -gt 0) {
            Write-Host "Ayue is already running (PID $($listeningProcessIds -join ', ')); matchmaker PID $($agentListeningProcessIds -join ', ')."
            exit 0
        }
    }
    else {
        foreach ($processId in $listeningProcessIds) {
            Write-Host "Stopping the old Ayue process (PID $processId)..."
            Stop-Process -Id $processId -Force
            Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
        }
        $listeningProcessIds = @()
    }
}

$agentProcess = $null
if ($agentListeningProcessIds.Count -eq 0) {
    $agentProcess = Start-HiddenPython -Arguments @("agent_api.py") -WorkingDirectory $agentDirectory
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        $newAgentProcessIds = @(Get-ListeningProcessIds -TargetPort $AgentPort)
        if ($agentProcess.HasExited -and $newAgentProcessIds.Count -eq 0) {
            throw "Ayue matchmaker failed to start (exit $($agentProcess.ExitCode))."
        }
        if ($newAgentProcessIds.Count -gt 0 -and (Test-MatchmakerHealth)) {
            $agentListeningProcessIds = $newAgentProcessIds
            break
        }
    }
    if ($agentListeningProcessIds.Count -eq 0) {
        Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue
        throw "Ayue matchmaker did not pass its health check within 30 seconds."
    }
}

if ($NoRestart -and $listeningProcessIds.Count -gt 0) {
    Write-Host "Ayue is already running (PID $($listeningProcessIds -join ', ')); matchmaker PID $($agentListeningProcessIds -join ', ')."
    exit 0
}

$uvicornArguments = @(
    "-m", "uvicorn", "main:app",
    "--host", "127.0.0.1",
    "--port", $Port.ToString()
)

if (-not $Background) {
    Write-Host "Starting the latest Ayue server: $healthUrl"
    Write-Host "Press Ctrl+C to stop."
    Push-Location $appDirectory
    try {
        & $pythonPath @uvicornArguments
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
        if ($agentProcess) {
            Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

$serverProcess = Start-HiddenPython -Arguments $uvicornArguments -WorkingDirectory $appDirectory

for ($attempt = 1; $attempt -le 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    $newListeningProcessIds = @(Get-ListeningProcessIds -TargetPort $Port)
    if ($serverProcess.HasExited -and $newListeningProcessIds.Count -eq 0) {
        throw "Ayue failed to start (exit $($serverProcess.ExitCode)). Run without -Background to see the server output."
    }
    if ($newListeningProcessIds.Count -gt 0 -and (Test-AyueHealth)) {
        Write-Host "Ayue started (PID $($newListeningProcessIds -join ', '), HTTP 200): $healthUrl"
        Write-Host "Matchmaker started (PID $($agentListeningProcessIds -join ', '), HTTP 200): $agentHealthUrl"
        Write-Host "Run without -Background when you need live server logs."
        exit 0
    }
}

foreach ($processId in @(Get-ListeningProcessIds -TargetPort $Port)) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}
Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
if ($agentProcess) {
    Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue
}
throw "Ayue did not pass its health check within 30 seconds. PID $($serverProcess.Id) was stopped."
