# Lossless — Windows setup helper
# Run with:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

$ErrorActionPreference = "Stop"

Write-Host "→ Installing Python deps…" -ForegroundColor Cyan
python -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "→ Created .env from .env.example. Edit it and re-run." -ForegroundColor Yellow
    Write-Host "  Required: GOOGLE_API_KEY (or GOOGLE_CLOUD_PROJECT)"
    Write-Host "  Optional: DT_ENVIRONMENT + DT_PLATFORM_TOKEN"
    exit 0
}

Write-Host "→ Starting Lossless on http://localhost:8080" -ForegroundColor Green
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
