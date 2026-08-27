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

# バージョンを "3.12" の形で返す。実際に動かして確かめる。
#
# ここで -c にコードを渡してはいけない。
# Windows PowerShell 5.1 はネイティブコマンドへ引数を渡す際に
# 文字列内の二重引用符を落としてしまうため、
#   import sys; print("%d.%d" % sys.version_info[:2])
# が Python 側では
#   import sys; print(%d.%d % sys.version_info[:2])
# となって SyntaxError で落ちる。
# その結果、正常にインストールされている Python まで
# 「動作せず」と誤判定していた (実機で発生)。
# --version なら引数が 1 語だけで引用符も不要なので、この問題を回避できる。
function Get-PyVersion($exe, $exeArgs) {
    try {
        $out = & $exe @exeArgs --version 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        $t = ($out | Out-String).Trim()
        if ($t -match 'Python\s+(\d+)\.(\d+)') { return "$($Matches[1]).$($Matches[2])" }
        return $null
    } catch { return $null }
}

$found = @()      # 実際に動いた Python
$probed = @()     # 試した場所と結果 (原因調査用にすべて記録する)
$sawStoreAlias = $false

function Try-Python($exe, $exeArgs, $label) {
    if ($script:found | Where-Object { $_.Exe -eq $exe -and (($_.PyArgs -join " ") -eq ($exeArgs -join " ")) }) { return }
    $ver = Get-PyVersion $exe $exeArgs
    $script:probed += [pscustomobject]@{ Label=$label; Ver=$ver }
    if ($ver) {
        $script:found += [pscustomobject]@{ Exe=$exe; PyArgs=$exeArgs; Ver=$ver; From=$label }
    }
}

# 1) レジストリ: Windows で「どの Python が入っているか」の最も確実な情報源。
#    PATH を通していなくても、インストーラが必ずここに書く。
foreach ($root in @("HKLM:\SOFTWARE\Python\PythonCore",
                    "HKCU:\SOFTWARE\Python\PythonCore",
                    "HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore")) {
    foreach ($key in (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
        $ip = (Get-ItemProperty "$($key.PSPath)\InstallPath" -ErrorAction SilentlyContinue)
        if (-not $ip) { continue }
        $dir = $ip.'(default)'
        if (-not $dir) { continue }
        $exe = Join-Path $dir "python.exe"
        if (Test-Path $exe) { Try-Python $exe @() $exe }
    }
}

# 2) py ランチャー。-0p で導入済みの Python を一覧できる。
if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $list = & py -0p 2>&1
        foreach ($line in ($list -split "`n")) {
            if ($line -match '([A-Za-z]:\\[^\r\n]*python\.exe)') {
                Try-Python $Matches[1].Trim() @() $Matches[1].Trim()
            }
        }
    } catch { }
    foreach ($v in @("-3.14","-3.13","-3.12","-3.11","-3.10","-3")) {
        Try-Python "py" @($v) "py $v"
    }
}

# 3) PATH 上の python / python3。
#    Microsoft Store のショートカットかどうかを見た目で判断せず、
#    まず実行してみる。Store 版が正しく入っていれば動くため、
#    パスだけで弾くと入っているのに「無い」と誤判定する。
foreach ($name in @("python","python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like "*WindowsApps*") { $sawStoreAlias = $true }
    Try-Python $cmd.Source @() $cmd.Source
}

# 4) よくあるインストール先を直接走査
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
        Try-Python $hit.FullName @() $hit.FullName
    }
}

# 3.10 以上のうち最も新しいものを採用
$usable = $found | Where-Object {
    $p = $_.Ver.Split(".")
    ([int]$p[0] -gt 3) -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 10)
} | Sort-Object { [version]$_.Ver } -Descending

if ($usable.Count -gt 0) {
    $py = $usable[0].Exe
    $pyArgs = $usable[0].PyArgs
    Ok "Python $($usable[0].Ver) を使用します"
    Write-Host "      $($usable[0].From)" -ForegroundColor DarkGray
} else {
    Write-Host ""
    if ($probed.Count -gt 0) {
        Write-Host "  調べた場所:" -ForegroundColor DarkGray
        foreach ($pr in $probed) {
            $r = if ($pr.Ver) { "Python $($pr.Ver)" } else { "動作せず" }
            Write-Host "        $r  <-  $($pr.Label)" -ForegroundColor DarkGray
        }
        Write-Host ""
    }
    if ($found.Count -gt 0) {
        Die @"
Python 3.10 以上が必要です (見つかったのは古い版だけでした)。

  https://www.python.org/downloads/ から新しい版を入れてください。
  インストール時に「Add python.exe to PATH」にチェックを入れます。
  古い版は残しておいて構いません。

  入れ終わったら PowerShell を閉じて開き直し、
  cd ttradar してからもう一度実行してください。
"@
    }
    $hint = ""
    if ($sawStoreAlias) {
        $hint = @"

  ※ PATH にある python は Microsoft Store のショートカットです。
     Python 本体ではないので、これだけでは動きません。

"@
    }
    Die @"
Python がインストールされていません。$hint
  インストール手順:

  1. https://www.python.org/downloads/ を開く
  2. 黄色い「Download Python 3.x.x」ボタンを押す
  3. ダウンロードした .exe を実行する
  4. 最初の画面の一番下にある
     「Add python.exe to PATH」に必ずチェックを入れる  ← 重要
  5. 「Install Now」を押し、完了するまで待つ
     (「Setup was successful」と出れば成功)

  よくある失敗:
    - Store のページが開いただけで、実際には入れていない
    - インストーラを起動したが「Install Now」を押していない
    - 手順 4 のチェックを入れ忘れた

  入れ終わったら PowerShell を閉じて開き直し、
  cd ttradar してからもう一度実行してください。
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
