<#
  推送器 v3（2026-10-13）—— 待切换版
  ============================================================
  管道：DH_export\native\  —— 抓取器复制来的 .dat/.dhsln（流程里没有任何 txt）

  ① 推送：把中转筐**复制**给盒子（同相对路径 → 覆盖旧版本）；
     成功后**删除自己那份中转副本**（PC 不留数据副本）；失败不删，下趟重试。
  ② 删除同步：读 _state\pending_delete.txt（抓取器 v2 记录的"源文件已消失"清单），
     ssh 让盒子把它们**移进 ~/dh-lab/_trash/<时间戳>/…**（不是硬删，可找回）；
     全部成功才清空清单；失败保留、下趟重试。
     安全阀：待删数量超过 (账本+待删) 的 20% → 中止（判为源目录异常）。

  切换方法：用本文件覆盖 dh_push.ps1（保持 UTF-8 BOM + CRLF），计划任务不用动。
#>
[CmdletBinding()]
param(
    [string]$Stage     = '',
    [string]$Box       = 'admin@10.201.9.180',
    [string]$RemoteDir = '/home/admin/dh-lab/inbox/',
    [int]$MinAgeSec    = 5,
    [switch]$KeepSent  = $false
)

$ErrorActionPreference = 'Continue'
if (-not $Stage) { $Stage = Join-Path $PSScriptRoot 'DH_export' }
$stateDir = Join-Path $Stage '_state'
$logPath  = Join-Path $stateDir 'dh_push.log'

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

    # 优先用 Windows 自带的 scp/ssh（Git 版 msys scp 会把反斜杠当转义符）
    $sysScp = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
    $scpExe = if (Test-Path -LiteralPath $sysScp) { $sysScp } else { (Get-Command scp -ErrorAction SilentlyContinue).Source }
    $sysSsh = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    $sshExe = if (Test-Path -LiteralPath $sysSsh) { $sysSsh } else { (Get-Command ssh -ErrorAction SilentlyContinue).Source }
    if (-not $scpExe) { Write-Log 'scp 不可用（请安装 Windows 的 OpenSSH 客户端）。'; exit 0 }

    $sentRoot = Join-Path $Stage '_sent'
    $stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'

    # ---- ① 推送 ----
    foreach ($pipe in @('native')) {
        $dir = Join-Path $Stage $pipe
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $items = @(Get-ChildItem -LiteralPath $dir -Force | Where-Object { $_.Name -notlike '.*' })
        if ($items.Count -eq 0) { continue }
        $items = @($items | Where-Object { (New-TimeSpan -Start $_.LastWriteTime -End (Get-Date)).TotalSeconds -ge $MinAgeSec })
        if ($items.Count -eq 0) { continue }

        $remote  = '{0}:{1}' -f $Box, $RemoteDir
        $srcGlob = ($dir -replace '\\', '/') + '/*'
        & $scpExe -pr -o StrictHostKeyChecking=no -o ConnectTimeout=15 $srcGlob $remote *>> $logPath
        if ($LASTEXITCODE -eq 0) {
            if ($KeepSent) {
                $dest = Join-Path $sentRoot ('{0}-{1}' -f $pipe, $stamp)
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                foreach ($it in $items) { Move-Item -LiteralPath $it.FullName -Destination $dest -Force -ErrorAction SilentlyContinue }
                Write-Log ("已推送并归档 {0} 项（{1}，-KeepSent）" -f $items.Count, $pipe)
            } else {
                foreach ($it in $items) { Remove-Item -LiteralPath $it.FullName -Recurse -Force -ErrorAction SilentlyContinue }
                Write-Log ("已推送并删除中转副本 {0} 项（{1}）" -f $items.Count, $pipe)
            }
        } else {
            Write-Log ("推送失败（{0}）scp 退出码 {1} —— 保持原样，下趟重试" -f $pipe, $LASTEXITCODE)
        }
    }

    # ---- ② 删除同步 ----
    $pendPath = Join-Path $stateDir 'pending_delete.txt'
    if ((Test-Path -LiteralPath $pendPath) -and $sshExe) {
        $pending = @(Get-Content -LiteralPath $pendPath -Encoding UTF8 | Where-Object { $_.Trim() })
        if ($pending.Count -gt 0) {
            $ledgerPath = Join-Path $stateDir 'grabbed.txt'
            $ledgerCount = 0
            if (Test-Path -LiteralPath $ledgerPath) {
                $ledgerCount = @(Get-Content -LiteralPath $ledgerPath -Encoding UTF8 | Where-Object { $_.Trim() }).Count
            }
            if (($ledgerCount + $pending.Count) -gt 10 -and $pending.Count -gt 0.2 * ($ledgerCount + $pending.Count)) {
                Write-Log ("删除同步中止：待删 {0} 个，超过阈值（疑似源目录异常），下趟再判" -f $pending.Count)
            } else {
                $trash = "/home/admin/dh-lab/_trash/$stamp"
                $q = [char]39
                $ok = 0
                $failed = 0
                foreach ($rel in $pending) {
                    $r = ($rel.Trim()) -replace '\\', '/'
                    if (-not $r) { continue }
                    $remoteDst  = "$trash/$r"
                    $remoteDir2 = $remoteDst.Substring(0, $remoteDst.LastIndexOf('/'))
                    $cmd = "mkdir -p $q$remoteDir2$q && mv -f $q/home/admin/dh-lab/inbox/$r$q $q$remoteDst$q"
                    & $sshExe -o StrictHostKeyChecking=no -o ConnectTimeout=15 $Box $cmd *>> $logPath
                    if ($LASTEXITCODE -eq 0) { $ok++ } else { $failed++ }
                }
                if ($failed -eq 0) {
                    Clear-Content -LiteralPath $pendPath
                    Write-Log ("删除同步完成：盒子侧已移入回收目录 {0} 项 → {1}" -f $ok, $trash)
                } else {
                    Write-Log ("删除同步部分失败（成功 {0} / 失败 {1}）—— 清单保留，下趟重试" -f $ok, $failed)
                }
            }
        }
    }
    exit 0
}
catch {
    Write-Log ("推送异常（下趟重试）：{0}" -f $_.Exception.Message)
    exit 0
}
