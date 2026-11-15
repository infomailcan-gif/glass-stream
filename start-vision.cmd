@echo off
rem Goruntu isleme servisini baslatir. (Once "npm start" ile sunucuyu ac.)
rem Metin modeli (mobileclip_blt.ts) calisma dizininden yuklendigi icin vision\models icinden calisir.
set PYTHONIOENCODING=utf-8
rem Ultralytics eksik paket kurmaya calismasin (onnxruntime-gpu bu ekran kartinda calismiyor)
set YOLO_AUTOINSTALL=False
cd /d "%~dp0vision\models"
"%USERPROFILE%\.glass-venv\Scripts\python.exe" ..\detector.py %*
