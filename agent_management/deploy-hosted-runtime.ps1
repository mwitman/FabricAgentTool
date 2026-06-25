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

function Test-DockerAvailable {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        return $false
    }
    try {
        docker version --format '{{.Server.Version}}' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
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
if (-not $AcrName -and $AcrLoginServer -match "^([^.]+)\.") {
    $AcrName = $Matches[1]
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

$dockerAvailable = Test-DockerAvailable
$az = Get-Command az -ErrorAction SilentlyContinue
if (-not $dockerAvailable -and -not $az) {
    throw "Docker is not available, and Azure CLI was not found. Install or start Docker, or install Azure CLI to build in ACR."
}
if (-not $dockerAvailable -and -not $AcrName) {
    throw "Docker is not available, so ACR remote build requires ACR_NAME in .env or -AcrName."
}
if (-not $az -and -not $SkipLogin -and $AcrName) {
    throw "Azure CLI is required for 'az acr login'. Install Azure CLI or rerun with -SkipLogin after logging in another way."
}

if ($Subscription) {
    Invoke-NativeCommand az account set --subscription $Subscription | Out-Null
}

if (-not $SkipLogin) {
    if ($AcrName -and $dockerAvailable) {
        Invoke-NativeCommand az acr login --name $AcrName | Out-Null
    } elseif ($dockerAvailable) {
        Invoke-NativeCommand docker login $AcrLoginServer
    }
}

$fullTags = $Tags | ForEach-Object { "$AcrLoginServer/$ImageName`:$_" }
$primaryTag = if ($fullTags.Count -gt 1) { $fullTags[1] } else { $fullTags[0] }

if (-not $dockerAvailable) {
    if ($SkipBuild) {
        throw "Docker is not available, so -SkipBuild cannot push an existing local image. Remove -SkipBuild to use 'az acr build'."
    }
    $imageArgs = @()
    foreach ($tag in $Tags) {
        $imageArgs += @("--image", "$ImageName`:$tag")
    }
    Write-Host "Docker is not available. Building in Azure Container Registry with 'az acr build'."
    Invoke-NativeCommand az acr build --registry $AcrName --platform $Platform @imageArgs $runtimeRoot
} elseif (-not $SkipBuild) {
    Push-Location $runtimeRoot
    try {
        $tagArgs = @()
        foreach ($tag in $fullTags) {
            $tagArgs += @("-t", $tag)
        }
        Invoke-NativeCommand docker build --platform $Platform @tagArgs .
    } finally {
        Pop-Location
    }
}

if ($dockerAvailable) {
    foreach ($tag in $fullTags) {
        Invoke-NativeCommand docker push $tag
    }
}

Write-Host "Hosted runtime image pushed:"
foreach ($tag in $fullTags) {
    Write-Host "  $tag"
}
Write-Host "Use this image for hosted-agent deployments: $primaryTag"