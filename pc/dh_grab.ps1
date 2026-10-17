<#
  东华原生数据「抓取器」v2（2026-10-13）—— 待切换版
  ============================================================
  作用：把**软件数据目录**里的 .dat/.dhsln **复制**进中转筐，交给推送器送到盒子。
  🚨 纪律：对源目录**只读** —— 只复制，绝不修改/移动/删除/重命名任何源文件。

  相比 v1（现在 PC 上跑的版本）新增"完全同步"：
    * 账本记「路径 + 大小 + 修改时间」→ 源文件**被改**过就重新抓取、重新推送；
    * 源文件**消失** → 记入 `_state\pending_delete.txt`，由推送器 v3 在盒子上移入回收目录；
    * 两个安全阀：源目录不可达直接跳过；「消失」数量超过 20% 时只记警告、不写待删清单。

  切换方法：用本文件覆盖 dh_grab.ps1（保持 UTF-8 BOM + CRLF 编码），不需要动计划任务。
#>
[CmdletBinding()]
param(
    [string]$SrcRoot   = '',
    [string]$Stage     = '',
    [string[]]$Exts    = @('.dat', '.dhsln'),
    [string[]]$Exclude = @(),
    [int]$MinAgeSec    = 60,
    [int]$MaxDepth     = 6
)

$ErrorActionPreference = 'Continue'
if (-not $Stage) { $Stage = Join-Path $PSScriptRoot 'DH_export\native' }
if (-not $Exclude -or $Exclude.Count -eq 0) { $Exclude = @((Split-Path $Stage -Parent)) }

$overrideFile = Join-Path $PSScriptRoot 'paths.txt'
if (Test-Path -LiteralPath $overrideFile) {
    $line = Get-Content -LiteralPath $overrideFile -Encoding UTF8 |
            Where-Object { $_.Trim() -ne '' } | Select-Object -First 1
    if ($line) { $SrcRoot = $line.Trim() }
}

$stageParent = Split-Path $Stage -Parent
$stateDir    = Join-Path $stageParent '_state'
$logPath     = Join-Path $stateDir 'dh_grab.log'
$ledgerPath  = Join-Path $stateDir 'grabbed.txt'
$sizePath    = Join-Path $stateDir 'sizes.txt'
$pendPath    = Join-Path $stateDir 'pending_delete.txt'

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $Stage, $stateDir | Out-Null

    if ([string]::IsNullOrWhiteSpace($SrcRoot)) {
        Write-Log '未配置软件数据目录（$SrcRoot / paths.txt）——本趟跳过。'
        exit 0
    }
    if (-not (Test-Path -LiteralPath $SrcRoot)) {
        Write-Log ("源目录不可达：{0} —— 本趟跳过（不动待删清单）" -f $SrcRoot)
        exit 0
    }
    $srcFull   = (Resolve-Path -LiteralPath $SrcRoot).Path.TrimEnd('\')
    $stageFull = (Resolve-Path -LiteralPath $stageParent).Path

    # 账本：path -> "size|mtimeTicks"（旧格式的裸路径先"认领"，下次写入时补齐）
    $ledger = @{}
    if (Test-Path -LiteralPath $ledgerPath) {
        foreach ($l in (Get-Content -LiteralPath $ledgerPath -Encoding UTF8)) {
            if (-not $l.Trim()) { continue }
            $p = $l -split "`t"
            if ($p.Count -ge 3) { $ledger[$p[2]] = ($p[0] + '|' + $p[1]) } else { $ledger[$l.Trim()] = 'ADOPT' }
        }
    }
    $prevSize = @{}
    if (Test-Path -LiteralPath $sizePath) {
        foreach ($l in (Get-Content -LiteralPath $sizePath -Encoding UTF8)) {
            $p = $l -split "`t"
            if ($p.Count -ge 2 -and $p[1]) { $prevSize[$p[1]] = [int64]$p[0] }
        }
    }

    # 候选：指定后缀、避开中转筐/归档、避开任何含 DH_export 的路径
    $cand = @(Get-ChildItem -LiteralPath $srcFull -Recurse -Depth $MaxDepth -File -ErrorAction SilentlyContinue |
              Where-Object {
                  $Exts -contains $_.Extension.ToLower() -and
                  $_.FullName -notlike '*DH_export*' -and
                  $_.FullName -notlike "$stageFull*"
              })

    $nowList = New-Object System.Collections.Generic.List[string]
    $seenNow = @{}
    $nNew = 0
    foreach ($f in $cand) {
        $path = $f.FullName
        $seenNow[$path] = $true
        $nowList.Add(("{0}`t{1}" -f $f.Length, $path))
        $key = ('{0}|{1}' -f $f.Length, $f.LastWriteTimeUtc.Ticks)
        if ($ledger.ContainsKey($path)) {
            $lv = $ledger[$path]
            if ($lv -notlike '*|*') { $ledger[$path] = $key; continue }   # 旧账本条目：认领当前状态，不重抓
            if ($lv -eq $key) { continue }                                # 内容未变 → 跳过
        }
        if (-not $prevSize.ContainsKey($path) -or $prevSize[$path] -ne $f.Length) { continue }  # 还没写稳
        $age = (New-TimeSpan -Start $f.LastWriteTime -End (Get-Date)).TotalSeconds
        if ($age -lt $MinAgeSec) { continue }                                        # 太新 → 下趟再说
        $rel  = $path.Substring($srcFull.Length).TrimStart('\')
        $dest = Join-Path $Stage $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
        Copy-Item -LiteralPath $path -Destination $dest -Force
        $ledger[$path] = $key
        $nNew++
    }
    $nowList | Set-Content -LiteralPath $sizePath -Encoding UTF8

    # 消失检测（安全阀：总数 >10 且消失占比 >20% 判为异常，不动待删清单）
    $missing = @($ledger.Keys | Where-Object { -not $seenNow.ContainsKey($_) -and -not (Test-Path -LiteralPath $_) })
    if (($ledger.Count + $missing.Count) -gt 10 -and $missing.Count -gt 0.2 * ($ledger.Count + $missing.Count)) {
        Write-Log ("警告：{0}/{1} 个已抓文件不可见 —— 跳过消失检测（防误删）" -f $missing.Count, ($ledger.Count + $missing.Count))
        $missing = @()
    }
    if ($missing.Count -gt 0) {
        $old = @()
        if (Test-Path -LiteralPath $pendPath) {
            $old = @(Get-Content -LiteralPath $pendPath -Encoding UTF8 | Where-Object { $_.Trim() })
        }
        $relMissing = @($missing | ForEach-Object { $_.Substring($srcFull.Length).TrimStart([char]92) })
        $all = @(@($old) + @($relMissing) | Select-Object -Unique)
        $all | Set-Content -LiteralPath $pendPath -Encoding UTF8
        foreach ($m in $missing) { $ledger.Remove($m) | Out-Null }
        Write-Log ("消失检测：{0} 个源文件已不在 → 记入 pending_delete（合计 {1} 个待盒子侧移除）" -f $missing.Count, $all.Count)
    }

    # 重写账本（统一成 size<TAB>mtime<TAB>路径）
    $outLines = New-Object System.Collections.Generic.List[string]
    foreach ($k in $ledger.Keys) {
        if (-not $ledger[$k]) {
            if (Test-Path -LiteralPath $k) {
                $fi = Get-Item -LiteralPath $k
                $outLines.Add(("{0}`t{1}`t{2}" -f $fi.Length, $fi.LastWriteTimeUtc.Ticks, $k))
            }
        } else {
            $v = $ledger[$k] -split '\|'
            $outLines.Add(("{0}`t{1}`t{2}" -f $v[0], $v[1], $k))
        }
    }
    $outLines | Set-Content -LiteralPath $ledgerPath -Encoding UTF8

    if ($nNew -gt 0) {
        Write-Log ("抓取完成：新复制/更新 {0} 个文件（候选 {1} 个）→ {2}" -f $nNew, $cand.Count, $Stage)
    }
    exit 0
}
catch {
    Write-Log ("抓取异常（本趟跳过，下趟重试）：{0}" -f $_.Exception.Message)
    exit 0
}
