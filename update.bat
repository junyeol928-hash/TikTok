@echo off
rem ttradar を最新版に更新 (ダブルクリックで実行)

cd /d "%~dp0"

echo.
echo  最新版を取得しています...
echo.

git pull

echo.
echo  ---------------------------------------------
echo  更新が終わりました
echo.
pause
