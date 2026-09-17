# Hippocampus 一键演示（F2，Windows PowerShell）：起代理 → 灌数据 → 三格式请求 → 跑评测 → 出结果表。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
#   $env:HIPPOCAMPUS_DEMO_PORT = "8899"; powershell -File scripts\demo.ps1
#
# 纪律：离线可跑（无 key 时代理回"离线回执"，记忆纪律照常）；不弹窗；只写数据根。

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Port = if ($env:HIPPOCAMPUS_DEMO_PORT) { $env:HIPPOCAMPUS_DEMO_PORT } else { "8765" }
$HomeDir = if ($env:HIPPOCAMPUS_DEMO_HOME) { $env:HIPPOCAMPUS_DEMO_HOME } else { Join-Path $env:TEMP ("hippo_demo_" + [guid]::NewGuid().ToString("N").Substring(0, 8)) }
$env:HIPPOCAMPUS_HOME = $HomeDir
if (-not $env:HIPPOCAMPUS_OFFLINE) { $env:HIPPOCAMPUS_OFFLINE = "1" }

Write-Host "== Hippocampus 一键演示 =="
Write-Host "数据根: $HomeDir"
Write-Host "端口:   $Port"
Write-Host ""

$Proc = $null
try {
    Write-Host "-- [1/5] doctor（体检）"
    & $Python -m hippocampus.cli doctor 2>&1 | Select-Object -First 20
    Write-Host ""

    Write-Host "-- [2/5] seed（灌入合成示例数据）"
    & $Python -m hippocampus.cli seed 2>&1 | Select-Object -Last 3
    Write-Host ""

    Write-Host "-- [3/5] 起代理（后台，端口 $Port）"
    $Log = Join-Path $HomeDir "proxy.log"
    $Proc = Start-Process -FilePath $Python -ArgumentList @("-m", "hippocampus.cli", "proxy", "--port", $Port) `
        -RedirectStandardOutput $Log -RedirectStandardError (Join-Path $HomeDir "proxy.err.log") `
        -PassThru -WindowStyle Hidden
    $Ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $Ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $Ready) {
        Write-Host "代理未就绪（可能缺 uvicorn：pip install 'hippocampus-agent[proxy]'）；跳过三格式请求，直接跑评测。"
    } else {
        $Token = ""
        $TokenFile = Join-Path $HomeDir "instance_token"
        if (Test-Path $TokenFile) { $Token = (Get-Content $TokenFile -Raw).Trim() }
        $Headers = @{ "content-type" = "application/json" }
        if ($Token) { $Headers["Authorization"] = "Bearer $Token" }

        Write-Host "-- [4/5] 三格式请求（chat / responses / anthropic）"
        $BodyChat = '{"model":"demo","messages":[{"role":"user","content":"我投简历有什么要求？"}]}'
        $BodyResp = '{"model":"demo","instructions":"你是助手","input":"我投简历有什么要求？"}'
        $BodyAnth = '{"model":"demo","max_tokens":128,"system":"你是助手","messages":[{"role":"user","content":"我投简历有什么要求？"}]}'
        Write-Host "  chat:"
        $RespChat = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -Headers $Headers -Body $BodyChat -UseBasicParsing).Content
        $RespChat.Substring(0, [Math]::Min(220, $RespChat.Length))
        Write-Host "  responses:"
        $RespResp = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/responses" -Method Post -Headers $Headers -Body $BodyResp -UseBasicParsing).Content
        $RespResp.Substring(0, [Math]::Min(220, $RespResp.Length))
        Write-Host "  anthropic:"
        $RespAnth = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/messages" -Method Post -Headers $Headers -Body $BodyAnth -UseBasicParsing).Content
        $RespAnth.Substring(0, [Math]::Min(220, $RespAnth.Length))
        Write-Host "  （无凭据时以上为『离线回执』：记忆的检索/注入/固化照常执行）"
    }
    Write-Host ""

    Write-Host "-- [5/5] 评测（20 题 + 记忆开/关对照 + 关键词基线）"
    & $Python -m hippocampus.cli demo --questions 20 --memories --baseline
    Write-Host ""
    Write-Host "== 演示结束（代理已停）=="
}
finally {
    if ($Proc -and -not $Proc.HasExited) { Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue }
}
