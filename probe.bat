@echo off
rem TikTok から実際に何が返るか調べる (収集が0件のとき)
rem ダブルクリックで実行。どこから起動しても自分の場所へ移動する。

cd /d "%~dp0"

if not exist ".venv\Scripts\ttradar.exe" (
    echo.
    echo  セットアップがまだ済んでいません。
    echo  先に setup.bat をダブルクリックしてください。
    echo.
    pause
    exit /b 1
)

echo.
echo  TikTok の通信を調べます。
echo  ブラウザが開きますが、そのまま触らずに待ってください。
echo  30秒ほどで自動的に終わります。
echo.

".venv\Scripts\ttradar.exe" probe --visible

echo.
echo  ---------------------------------------------
echo  reports フォルダの中に
echo    probe_report.json
echo    probe_screenshot.png
echo  ができています。この2つを共有してください。
echo.
pause
