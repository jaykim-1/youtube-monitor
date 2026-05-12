# Reads .env and uploads required keys to GitHub Secrets via gh CLI.
# Values are never printed to the console.

$ErrorActionPreference = "Stop"

$ghDir = "C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin"
$env:Path = "$ghDir;$env:Path"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$EnvFile = Join-Path $ProjectDir ".env"

if (-not (Test-Path $EnvFile)) {
    Write-Error ".env not found at $EnvFile"
    exit 1
}

# Keys we need on GitHub Actions
$RequiredKeys = @(
    "YOUTUBE_API_KEY",
    "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID"
)

$OptionalKeys = @(
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "NOTIFY_TO"
)

# Parse .env into hashtable
$envMap = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        # Strip optional surrounding quotes
        if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
            $value = $Matches[1]
        }
        $envMap[$name] = $value
    }
}

function Set-Secret {
    param([string]$Name, [bool]$Required)

    if (-not $envMap.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace($envMap[$Name])) {
        if ($Required) {
            Write-Host "[SKIP] $Name (not set in .env) - REQUIRED, please fill in .env first" -ForegroundColor Red
            return $false
        } else {
            Write-Host "[SKIP] $Name (not set in .env) - optional, skipping" -ForegroundColor DarkGray
            return $true
        }
    }

    $value = $envMap[$Name]
    # Pipe value via stdin so it never appears in command line / history
    $value | gh secret set $Name --repo jaykim-1/youtube-monitor | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK]   $Name" -ForegroundColor Green
        return $true
    } else {
        Write-Host "[FAIL] $Name" -ForegroundColor Red
        return $false
    }
}

$ok = $true
foreach ($k in $RequiredKeys) {
    if (-not (Set-Secret -Name $k -Required $true)) { $ok = $false }
}
foreach ($k in $OptionalKeys) {
    Set-Secret -Name $k -Required $false | Out-Null
}

Write-Host ""
if ($ok) {
    Write-Host "All required secrets uploaded." -ForegroundColor Green
} else {
    Write-Host "Some required secrets are missing - fix .env and re-run." -ForegroundColor Yellow
    exit 1
}
