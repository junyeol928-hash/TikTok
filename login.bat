@echo off
rem TikTok にログインした状態を覚えさせる (収集が 0 件のとき)

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
echo  TikTok をブラウザで開きます。
echo  開いた画面でいつものアカウントにログインしてください。
echo  ログインできたら、このウィンドウで Enter を押してください。
echo.
echo  一度ログインすれば、次からの収集でもその状態が使われます。
echo.

".venv\Scripts\ttradar.exe" login

echo.
pause
