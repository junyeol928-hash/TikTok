# ttradar セットアップ (Windows / PowerShell)
#
#   PowerShell を開いてこのフォルダで:
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# 仮想環境の作成・依存のインストール・Chromium の取得・設定ファイル生成・
# 動作診断までを一度に行う。何度実行しても壊れない。

# Stop にすると、native コマンド (pip 等) が stderr に何か書いただけで
# Windows PowerShell 5.1 が NativeCommandError として異常終了させてしまう。
# 成否は $LASTEXITCODE で明示的に判定するので Continue のままにする。
$ErrorActionPreference = "Continue"

function Step($m) { Write-Host ""; Write-Host "▸ $m" -ForegroundColor White }
function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !   $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host ""; Write-Host "NG  $m" -ForegroundColor Red; Write-Host ""; exit 1 }

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "ttradar セットアップ" -ForegroundColor White
Write-Host "TikTok 商品紹介トレンドレーダー" -ForegroundColor DarkGray

# ------------------------------------------------------------------ Python
Step "Python を確認しています"

# バージョンを "3.12" の形で返す。取れなければ $null。
$verProbe = 'import sys; print("%d.%d" % sys.version_info[:2])'
function Get-PyVersion($exe, $exeArgs) {
    try {
        $out = & $exe @exeArgs -c $verProbe 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        $t = ($out | Out-String).Trim()
        if ($t -match '(\d+)\.(\d+)') { return "$($Matches[1]).$($Matches[2])" }
        return $null
    } catch { return $null }
}

# Microsoft Store のダミー (App Execution Alias) を見分ける。
# PATH には載っているが Python 本体ではなく、実行すると Store が開くだけ。
function Test-StoreStub($path) {
    if (-not $path) { return $false }
    if ($path -notlike "*WindowsApps*") { return $false }
    try { return ((Get-Item $path -ErrorAction Stop).Length -eq 0) } catch { return $true }
}

$found = @()          # 見つかった Python 全部 (古いものも含む)
$storeStub = $false

# 1) py ランチャー (公式インストーラが入れる。PATH が通っていなくても使える)
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.14","-3.13","-3.12","-3.11","-3.10","-3")) {
        $ver = Get-PyVersion "py" @($v)
        if ($ver) { $found += [pscustomobject]@{ Exe="py"; PyArgs=@($v); Ver=$ver; From="py $v" } }
    }
}

# 2) PATH 上の python / python3 (Store のダミーは除外)
foreach ($name in @("python","python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $src = $cmd.Source
    if (Test-StoreStub $src) { $storeStub = $true; continue }
    $ver = Get-PyVersion $src @()
    if ($ver) { $found += [pscustomobject]@{ Exe=$src; PyArgs=@(); Ver=$ver; From=$name } }
}

# 3) よくあるインストール先を直接探す
#    「Add python.exe to PATH」を外してインストールした場合、
#    Python は入っているのに PATH からは見えない。これが最も多い。
$globs = @(
    "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
    "$env:ProgramFiles\Python3*\python.exe",
    "${env:ProgramFiles(x86)}\Python3*\python.exe",
    "$env:LOCALAPPDATA\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3*\python.exe",
    "C:\Python3*\python.exe"
)
foreach ($g in $globs) {
    if (-not $g) { continue }
    foreach ($hit in (Get-ChildItem $g -ErrorAction SilentlyContinue)) {
        if ($found | Where-Object { $_.Exe -eq $hit.FullName }) { continue }
        $ver = Get-PyVersion $hit.FullName @()
        if ($ver) { $found += [pscustomobject]@{ Exe=$hit.FullName; PyArgs=@(); Ver=$ver; From=$hit.FullName } }
    }
}

# 3.10 以上のうち最も新しいものを選ぶ
$usable = $found | Where-Object {
    $p = $_.Ver.Split(".")
    ([int]$p[0] -gt 3) -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 10)
} | Sort-Object { [version]$_.Ver } -Descending

if ($usable.Count -gt 0) {
    $py = $usable[0].Exe
    $pyArgs = $usable[0].PyArgs
    Ok "Python $($usable[0].Ver) を使用します"
    if ($usable[0].From -like "*\*") { Write-Host "      $($usable[0].From)" -ForegroundColor DarkGray }
} else {
    Write-Host ""
    if ($found.Count -gt 0) {
        Warn "見つかった Python (いずれも 3.10 未満):"
        foreach ($f in $found) { Write-Host "        $($f.Ver)  $($f.From)" -ForegroundColor DarkGray }
        Write-Host ""
        Die @"
Python 3.10 以上が必要です。

  https://www.python.org/downloads/ から新しい版を入れてください。
  インストール時に「Add python.exe to PATH」にチェックを入れます。
  古い版はそのまま残しておいて構いません。

  入れ終わったら PowerShell を閉じて開き直し、もう一度実行してください。
"@
    }
    if ($storeStub) {
        Die @"
Python がインストールされていません。

  PATH にある python は Microsoft Store のショートカットで、
  Python 本体ではありません (実行すると Store が開くだけです)。

  次のどちらかで入れてください。

  A) 公式サイト (おすすめ)
     1. https://www.python.org/downloads/ を開く
     2. 黄色い「Download Python 3.x.x」ボタンを押す
     3. インストーラを起動し、最初の画面の下にある
        「Add python.exe to PATH」に必ずチェックを入れる
     4. 「Install Now」を押す

  B) Microsoft Store
     Store で「Python 3.12」を検索してインストール

  入れ終わったら PowerShell を閉じて開き直し、
  もう一度このスクリプトを実行してください。
"@
    }
    Die @"
Python が見つかりません。

  1. https://www.python.org/downloads/ を開く
  2. 黄色い「Download Python 3.x.x」ボタンを押す
  3. インストーラを起動し、最初の画面の下にある
     「Add python.exe to PATH」に必ずチェックを入れる
  4. 「Install Now」を押す

  入れ終わったら PowerShell を閉じて開き直し、
  もう一度このスクリプトを実行してください。
"@
}

# ------------------------------------------------------------------ 仮想環境
Step "仮想環境を作成しています (.venv)"
if (-not (Test-Path ".venv")) {
    & $py @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "仮想環境を作成できませんでした" }
    Ok "作成しました"
} else {
    Ok "既存のものを使用します"
}
# Windows の仮想環境は Scripts\ 配下 (Mac/Linux の bin\ ではない)
$vpy = ".\.venv\Scripts\python.exe"
$vtt = ".\.venv\Scripts\ttradar.exe"
if (-not (Test-Path $vpy)) { Die ".venv が壊れています。.venv フォルダを削除してやり直してください" }

# ------------------------------------------------------------------ 依存
Step "必要なライブラリをインストールしています (数分かかります)"
& $vpy -m pip install --quiet --upgrade pip | Out-Null
& $vpy -m pip install --quiet -e ".[browser]"
if ($LASTEXITCODE -ne 0) { Die "ライブラリのインストールに失敗しました。ネットワーク接続を確認してください" }
Ok "インストール完了"

# ------------------------------------------------------------------ ブラウザ
Step "Chromium を取得しています (初回のみ・約200MB)"
& $vpy -m playwright install chromium | Out-Null
if ($LASTEXITCODE -eq 0) {
    Ok "取得完了"
} else {
    Warn "Chromium の取得に失敗しました"
    Warn "後で手動で:  .\.venv\Scripts\python.exe -m playwright install chromium"
}

# ------------------------------------------------------------------ 設定
if (-not (Test-Path $vtt)) {
    Die "ttradar のインストールに失敗しています。
  上に出ているエラーメッセージを確認してください。"
}
Step "設定ファイルを用意しています"
if (-not (Test-Path "config.yaml")) {
    & $vtt init | Out-Null
    Ok "config.yaml を作成しました"
} else {
    Ok "config.yaml は既にあります"
}

# ------------------------------------------------------------------ 診断
Step "動作を診断しています"
Write-Host ""
& $vtt doctor
$doctor = $LASTEXITCODE

Write-Host ""
Write-Host "────────────────────────────────────────────" -ForegroundColor White
if ($doctor -eq 0) {
    Write-Host "TikTok に接続できました。そのまま使えます。" -ForegroundColor Green
    Write-Host ""
    Write-Host "  次のコマンドでアプリが開きます:"
    Write-Host "    .\.venv\Scripts\ttradar.exe serve --interval 120 --collect-now" -ForegroundColor White
} else {
    Write-Host "TikTok に接続できませんでした。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  上の [TikTok への到達性] の欄を確認してください。"
    Write-Host "  会社/学校のネットワークや VPN が原因のことが多いです。"
    Write-Host ""
    Write-Host "  接続できなくても、サンプルデータで画面は確認できます:"
    Write-Host "    .\.venv\Scripts\ttradar.exe demo" -ForegroundColor White
}
Write-Host ""
Write-Host "  アプリを止めるときは Ctrl+C" -ForegroundColor DarkGray
Write-Host ""
