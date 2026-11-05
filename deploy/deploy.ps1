<#
Push the committed code to the Vultr VM and run setup.sh there. Run from the repo root on Windows:

    powershell -File deploy\deploy.ps1 -Server 203.0.113.10
    powershell -File deploy\deploy.ps1 -Server 203.0.113.10 -PushKey   # also send the inference key

Only what is committed is sent (git archive HEAD): the git-ignored .env never leaves this machine,
except that with -PushKey the VULTR_INFERENCE_API_KEY line from .env goes over SSH into
/etc/hufu/secrets.env (mode 600). The key is never printed.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$User = "root",
    [string]$Key = "$HOME\.ssh\id_ed25519",
    [switch]$PushKey
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$dirty = git status --porcelain -- app sandbox deploy pyproject.toml uv.lock
if ($dirty) { Write-Warning "Uncommitted changes in app/, sandbox/ or deploy/ are NOT sent. Commit first." }

$archive = Join-Path $env:TEMP "hufu-deploy.tar.gz"
git archive --format=tar.gz -o $archive HEAD
$target = "$User@$Server"
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

Write-Host "== copying code to $Server"
scp @ssh $archive "${target}:/tmp/hufu.tar.gz"
ssh @ssh $target "rm -rf /opt/hufu.new && mkdir -p /opt/hufu.new && tar -xzf /tmp/hufu.tar.gz -C /opt/hufu.new && rm -rf /opt/hufu.old && (test -d /opt/hufu && mv /opt/hufu /opt/hufu.old || true) && mv /opt/hufu.new /opt/hufu && rm /tmp/hufu.tar.gz"

if ($PushKey) {
    $line = Get-Content (Join-Path $repo ".env") -ErrorAction Stop | Where-Object { $_ -match '^\s*VULTR_INFERENCE_API_KEY\s*=\s*\S' } | Select-Object -First 1
    if (-not $line) { throw "No VULTR_INFERENCE_API_KEY=... line in .env" }
    Write-Host "== sending the inference key (not shown)"
    $line.Trim() | ssh @ssh $target "mkdir -p /etc/hufu && umask 077 && cat > /etc/hufu/secrets.env"
}

Write-Host "== running setup.sh on the VM (first run takes a few minutes)"
ssh @ssh $target "bash /opt/hufu/deploy/setup.sh"
Remove-Item $archive -ErrorAction SilentlyContinue
