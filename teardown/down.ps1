# Remove the AWS resources recorded in this repo's Terraform state.
# The site directory goes first, then the health checker.
# Resources outside those two state files are left in place.
#
# From the repo root:
#   powershell -File teardown\down.ps1
#   powershell -File teardown\down.ps1 -WorkloadOnly
#   powershell -File teardown\down.ps1 -CheckerOnly

[CmdletBinding()]
param(
    [switch]$WorkloadOnly,
    [switch]$CheckerOnly
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

if ($WorkloadOnly -and $CheckerOnly) {
    throw "Choose -WorkloadOnly or -CheckerOnly, or pass neither to remove both stacks."
}

$Root = Split-Path -Parent $PSScriptRoot
$Stacks = @()
if (-not $CheckerOnly) {
    $Stacks += [pscustomobject]@{ Name = "site directory"; Dir = Join-Path $Root "terraform\lab" }
}
if (-not $WorkloadOnly) {
    $Stacks += [pscustomobject]@{ Name = "health checker"; Dir = Join-Path $Root "terraform" }
}

function Find-Terraform {
    $onPath = Get-Command terraform -ErrorAction SilentlyContinue
    if ($onPath) {
        return $onPath.Source
    }
    $winget = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") -Filter terraform.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($winget) {
        return $winget.FullName
    }
    throw "terraform.exe was not found on PATH or under the WinGet packages folder."
}

function Invoke-StackDestroy {
    param(
        [string]$Terraform,
        [string]$Name,
        [string]$Dir
    )
    $state = Join-Path $Dir "terraform.tfstate"
    if (-not (Test-Path $state)) {
        Write-Host "$Name has no Terraform state. Skipping."
        return
    }
    Write-Host "Destroying $Name from $Dir"
    Push-Location $Dir
    try {
        & $Terraform init -input=false
        if ($LASTEXITCODE -ne 0) {
            throw "$Name init failed with exit code $LASTEXITCODE."
        }
        & $Terraform destroy -auto-approve -input=false
        if ($LASTEXITCODE -ne 0) {
            throw "$Name destroy failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

$terraform = Find-Terraform
Write-Host "Using $terraform"
foreach ($stack in $Stacks) {
    Invoke-StackDestroy -Terraform $terraform -Name $stack.Name -Dir $stack.Dir
}
Write-Host "Teardown finished."
