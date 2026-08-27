@echo off
rem 1回だけ収集して結果を画面で確認する
rem ダブルクリックで実行。

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
echo  収集を開始します。数分かかります。
echo.

".venv\Scripts\ttradar.exe" collect

echo.
echo  ---------------------------------------------
pause
