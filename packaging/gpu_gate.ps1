# Future Windows GPU-runner gate. Not run by Linux remediation or ordinary tests.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force gpu-evidence | Out-Null
$status = 'RUN (FAIL)'
try {
    if (-not $IsWindows) { throw 'GPU gate requires a Windows runner' }
    if (-not $env:VIDLIFT_GPU_TEST_INPUT -or -not (Test-Path -PathType Leaf $env:VIDLIFT_GPU_TEST_INPUT)) { throw 'Provision a licensed synthetic speech fixture in VIDLIFT_GPU_TEST_INPUT' }
    if (-not $env:VIDLIFT_GPU_TEST_MODEL -or -not (Test-Path -PathType Container $env:VIDLIFT_GPU_TEST_MODEL)) { throw 'Provision the reviewed offline model in VIDLIFT_GPU_TEST_MODEL; this gate never downloads models' }
    if (-not (Test-Path (Join-Path $env:VIDLIFT_GPU_TEST_MODEL 'model.bin'))) { throw 'Offline model.bin missing' }
    $setup = @(Get-ChildItem gpu-artifact -Filter '*Setup.exe' -File)
    if ($setup.Count -ne 1) { throw 'Expected exactly one built installer' }
    $expected = ((Get-Content "$($setup[0].FullName).sha256" -Raw) -split '\s+')[0]
    if ((Get-FileHash $setup[0].FullName -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw 'GPU artifact SHA-256 mismatch' }
    $root = Join-Path $env:RUNNER_TEMP ('vidlift-gpu-' + [guid]::NewGuid().ToString('N'))
    $install = Join-Path $root 'installed'
    $installArgument = '/DIR="{0}"' -f $install
    New-Item -ItemType Directory -Force $root | Out-Null
    $env:VIDLIFT_STATE_DIR = Join-Path $root 'state'
    $env:VIDLIFT_SETTINGS_ORG = 'VidLift-CI-GPU-' + [guid]::NewGuid().ToString('N')
    $env:VIDLIFT_MONITOR_AUTOSTART = '0'
    $p = Start-Process $setup[0].FullName -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', $installArgument) -PassThru -Wait
    if ($p.ExitCode -ne 0) { throw 'GPU-runner isolated installation failed' }
    $cli = Join-Path $install 'vidlift-cli.exe'
    & $cli --selftest | Tee-Object gpu-evidence\selftest.json
    if ($LASTEXITCODE -ne 0) { throw 'Frozen GPU selftest failed' }
    $selftest = ((Get-Content gpu-evidence\selftest.json -Raw) -replace '^vidlift-selftest\s+', '') | ConvertFrom-Json
    if ($selftest.cuda_runtime.status -ne 'ok') { throw 'GPU runner did not qualify CUDA' }
    $input = Join-Path $root ('synthetic-speech' + [IO.Path]::GetExtension($env:VIDLIFT_GPU_TEST_INPUT))
    Copy-Item -LiteralPath $env:VIDLIFT_GPU_TEST_INPUT -Destination $input
    $before = (Get-FileHash $input -Algorithm SHA256).Hash
    & $cli subs $input --device cuda --model $env:VIDLIFT_GPU_TEST_MODEL --lang en --force | Tee-Object gpu-evidence\transcription.txt
    if ($LASTEXITCODE -ne 0) { throw 'Frozen CUDA transcription failed' }
    $text = Get-Content gpu-evidence\transcription.txt -Raw
    if ($text -notmatch '(?m)^  device: cuda\s*$') { throw 'Actual transcription device was not CUDA (CPU fallback is not a GPU pass)' }
    if ($text -notmatch '(?m)^  cues: [1-9][0-9]*') { throw 'Speech fixture did not yield subtitle cues' }
    if ((Get-FileHash $input -Algorithm SHA256).Hash -ne $before) { throw 'Synthetic input changed' }
    $stem = Join-Path $root 'synthetic-speech'
    if (-not (Test-Path "$stem.subs.mp4") -or -not (Test-Path "$stem.subs.srt")) { throw 'Frozen GPU outputs missing' }
    $status = 'RUN (PASS)'
} finally {
    @{ gpu_gate = $status } | ConvertTo-Json | Set-Content gpu-evidence\gpu-gate.json
    "gpu_gate: $status" | Out-File $env:GITHUB_STEP_SUMMARY -Append
}
