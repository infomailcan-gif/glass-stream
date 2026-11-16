@echo off
chcp 65001 >nul
title Canberk Sikli AI Systems
cd /d "%~dp0"

rem Sunucu zaten calisiyor mu? (port 8000)
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul
if %errorlevel%==0 (
  echo Sunucu zaten calisiyor, yeniden baslatilmadi.
) else (
  echo [1/2] Sunucu baslatiliyor...
  start "AI Systems - Sunucu" /min cmd /k node server.js
  timeout /t 2 /nobreak >nul
)

rem Yapay zeka servisi zaten calisiyor mu? Iki kopya ekran kartini paylasip birbirini yavaslatir.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -match 'detector\.py') { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo Yapay zeka servisi zaten calisiyor, yeniden baslatilmadi.
) else (
  echo [2/2] Yapay zeka servisi baslatiliyor ^(modeller ~20 sn'de yuklenir^)...
  start "AI Systems - Yapay Zeka" /min "%~dp0start-vision.cmd"
)

timeout /t 3 /nobreak >nul
start "" http://localhost:8000
echo.
echo Hazir. Web paneli tarayicida acildi.
echo Kapatmak icin "AI Systems - Sunucu" ve "AI Systems - Yapay Zeka" pencerelerini kapat.
timeout /t 6 >nul
