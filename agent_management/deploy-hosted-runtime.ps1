param(
    [string]$AcrName = "",
    [string]$AcrLoginServer = "",
    [string]$Subscription = "",
    [string]$ImageName = "hosted-agent-runtime",
    [string[]]$Tags = @(),
    [string]$Platform = "linux/amd64",
    [switch]$SkipBuild,
    [switch]$SkipLogin
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $name, $value = $line.Split("=", 2)
        $values[$name.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $values
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $scriptRoot "hosted_agent_runtime"
$envValues = Read-DotEnv (Join-Path $scriptRoot ".env")

if (-not $AcrName -and $envValues.ContainsKey("ACR_NAME")) {
    $AcrName = $envValues["ACR_NAME"]
}
if (-not $AcrLoginServer -and $envValues.ContainsKey("ACR_LOGIN_SERVER")) {
    $AcrLoginServer = $envValues["ACR_LOGIN_SERVER"]
}
if (-not $AcrLoginServer -and $AcrName) {
    $AcrLoginServer = "$AcrName.azurecr.io"
}
if (-not $AcrLoginServer) {
    throw "ACR login server is required. Set ACR_LOGIN_SERVER in .env or pass -AcrLoginServer."
}
if (-not (Test-Path (Join-Path $runtimeRoot "Dockerfile"))) {
    throw "Hosted runtime Dockerfile was not found at $runtimeRoot."
}

if (-not $Tags -or $Tags.Count -eq 0) {
    $timestampTag = "dt-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $Tags = @("latest", $timestampTag)
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    throw "Docker is required to build and push the hosted runtime image."
}

$az = Get-Command az -ErrorAction SilentlyContinue
if (-not $az -and -not $SkipLogin) {
    throw "Azure CLI is required for 'az acr login'. Install Azure CLI or rerun with -SkipLogin after logging in another way."
}

if ($Subscription) {
    az account set --subscription $Subscription | Out-Null
}

if (-not $SkipLogin) {
    if ($AcrName) {
        az acr login --name $AcrName | Out-Null
    } else {
        docker login $AcrLoginServer
    }
}

$fullTags = $Tags | ForEach-Object { "$AcrLoginServer/$ImageName`:$_" }
$primaryTag = if ($fullTags.Count -gt 1) { $fullTags[1] } else { $fullTags[0] }

if (-not $SkipBuild) {
    Push-Location $runtimeRoot
    try {
        $tagArgs = @()
        foreach ($tag in $fullTags) {
            $tagArgs += @("-t", $tag)
        }
        docker build --platform $Platform @tagArgs .
    } finally {
        Pop-Location
    }
}

foreach ($tag in $fullTags) {
    docker push $tag
}

Write-Host "Hosted runtime image pushed:"
foreach ($tag in $fullTags) {
    Write-Host "  $tag"
}
Write-Host "Use this image for hosted-agent deployments: $primaryTag"