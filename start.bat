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
echo  収集のたびに TikTok のブラウザ画面が開きますが、
echo  触らずに放置してください。自動で閉じます。
echo.
echo  最初の3回は15分おき、そのあとは2時間おきに自動収集します。
echo  (伸び率を出すには2回以上の収集が必要なため)
echo  次の収集までの時間はアプリの左下に出ます。
echo.
echo  終了するには、このウィンドウで Ctrl+C を押してください
echo.

".venv\Scripts\ttradar.exe" serve --interval 120 --collect-now --visible

echo.
pause
