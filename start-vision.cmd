@echo off
rem Goruntu isleme servisini baslatir. (Once "npm start" ile sunucuyu ac.)
rem Metin modeli (mobileclip_blt.ts) calisma dizininden yuklendigi icin vision\models icinden calisir.
set PYTHONIOENCODING=utf-8
cd /d "%~dp0vision\models"
"%USERPROFILE%\.glass-venv\Scripts\python.exe" ..\detector.py %*
