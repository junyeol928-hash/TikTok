@echo off
rem ttradar 起動 (ダブルクリックで実行)

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
echo  ttradar を起動します
echo  ブラウザが自動で開きます
echo.
echo  終了するには、このウィンドウで Ctrl+C を押してください
echo.

".venv\Scripts\ttradar.exe" serve --interval 120 --collect-now

echo.
pause
