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
$py = $null
foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "py -3", "python")) {
    # $args は PowerShell の自動変数なので代入してはいけない ($cmdArgs を使う)
    $parts = $cand.Split(" ")
    $exe = $parts[0]
    $cmdArgs = if ($parts.Count -gt 1) { $parts[1..($parts.Count-1)] } else { @() }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $check = & $exe @cmdArgs -c "import sys; print(1 if sys.version_info>=(3,10) else 0)" 2>$null
        if ("$check".Trim() -eq "1") { $py = $exe; $pyArgs = $cmdArgs; break }
    } catch { }
}
if (-not $py) {
    Die @"
Python 3.10 以上が見つかりません。

  1) https://www.python.org/downloads/ からインストーラをダウンロード
  2) インストール時に「Add python.exe to PATH」に必ずチェックを入れる
  3) PowerShell を開き直して、もう一度このスクリプトを実行

  ※ Microsoft Store 版の Python でも動きます。
"@
}
$ver = & $py @pyArgs --version
Ok "$ver を使用します"

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
