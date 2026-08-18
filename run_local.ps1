# Local supervisor: keeps Reaper alive across chaos kills.
# The point of /chaos/kill is that the PROCESS dies and durable state survives;
# on Cloud Run the platform restarts the container — locally, this loop does.
Set-Location $PSScriptRoot
while ($true) {
    .\.venv\Scripts\uvicorn.exe main:api --port 8080
    Write-Host "`n[reaper] process exited - resurrecting in 1s (Ctrl+C twice to stop)" -ForegroundColor Yellow
    Start-Sleep -Seconds 1
}
