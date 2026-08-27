@echo off
rem ttradar セットアップ (ダブルクリックで実行)
rem このファイルは日本語 Windows の cmd が読めるよう CP932 で保存している

cd /d "%~dp0"

echo.
echo  ttradar のセットアップを開始します
echo  完了まで 5-10 分ほどかかります
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"

echo.
echo  ---------------------------------------------
echo  このウィンドウは閉じて構いません
echo.
pause
