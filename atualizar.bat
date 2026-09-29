@echo off
chcp 65001 >nul
title Baixa - atualizar
cd /d "%~dp0"
echo Atualizando o motor de download (yt-dlp)...
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
echo.
".venv\Scripts\python.exe" -m yt_dlp --version
echo Pronto. Feche e abra o Baixa de novo (iniciar.bat).
pause
