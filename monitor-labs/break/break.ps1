$ErrorActionPreference = "Stop"
$FunctionName = $env:LAB_FUNCTION_NAME
$ApiUrl = $env:LAB_API_URL
if ([string]::IsNullOrWhiteSpace($FunctionName) -or [string]::IsNullOrWhiteSpace($ApiUrl)) {
    throw "working outputs were not provided"
}
if ($FunctionName -like "sre-agent*" -or $FunctionName -like "enterprise-sites*") {
    throw "refusing to change a protected function"
}

aws lambda update-function-configuration --function-name $FunctionName --region us-east-2 --environment "Variables={TABLE_NAME=missing-table}" --no-cli-pager | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
aws lambda wait function-updated --function-name $FunctionName --region us-east-2
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$deadline = (Get-Date).AddMinutes(4)
$sawError = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -Uri $ApiUrl -Method GET -UseBasicParsing | Out-Null
    } catch {
        if ($null -eq $_.Exception.Response) { throw }
    }
    Start-Sleep -Seconds 20
    $start = (Get-Date).ToUniversalTime().AddMinutes(-15).ToString("yyyy-MM-ddTHH:mm:ssZ")
    $end = (Get-Date).ToUniversalTime().AddMinutes(1).ToString("yyyy-MM-ddTHH:mm:ssZ")
    $raw = aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors --dimensions "Name=FunctionName,Value=$FunctionName" --start-time $start --end-time $end --period 60 --statistics Sum --region us-east-2 --output json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $points = @((($raw | ConvertFrom-Json).Datapoints))
    $total = 0.0
    foreach ($point in $points) {
        if ($null -ne $point.Sum) { $total += [double]$point.Sum }
    }
    Write-Output "errors_sum $total"
    if ($total -gt 0) {
        $sawError = $true
        break
    }
}
if (-not $sawError) {
    throw "CloudWatch Errors for $FunctionName stayed at zero"
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPath = Join-Path $RepoRoot "config.local.json"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "local config was not found"
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$account = [string]$config.aws_account_id
$region = [string]$config.aws_region
if ([string]::IsNullOrWhiteSpace($account) -or [string]::IsNullOrWhiteSpace($region)) {
    throw "account and region are missing from local config"
}
Push-Location $RepoRoot
try {
    Remove-Item Env:PYTEST_CURRENT_TEST -ErrorAction SilentlyContinue
    $env:PYTHONPATH = Join-Path $RepoRoot "src"
    python -m sre_agent inventory --account $account --region $region
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
