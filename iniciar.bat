@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Baixa - downloader de videos
cd /d "%~dp0"

rem Para testar sem instalar nada de verdade: set BAIXA_DRYRUN=1 antes de rodar.
set "WINGET=winget"
if defined BAIXA_DRYRUN set "WINGET=echo    [simulacao] winget"

rem ---------- 1. ferramentas necessarias (instala o que faltar, so na primeira vez) ----------
set "FALTA="
call :find_python || set "FALTA=1"
where ffmpeg >nul 2>nul || set "FALTA=1"
where node >nul 2>nul || set "FALTA=1"
if defined FALTA call :install_tools

call :find_python
if errorlevel 1 (
  echo.
  echo Nao encontrei o Python. Instale em https://www.python.org/downloads/
  echo marcando a opcao "Add python.exe to PATH" e abra o iniciar.bat de novo.
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul || echo AVISO: ffmpeg nao encontrado - 1080p, MP3 e capa nao vao funcionar. Feche e abra o iniciar.bat de novo.
where node >nul 2>nul || echo AVISO: Node.js nao encontrado - o YouTube pode nao funcionar. Feche e abra o iniciar.bat de novo.

rem ---------- 2. ambiente do app (so na primeira vez) ----------
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Preparando o Baixa pela primeira vez, aguarde...
  "%PYEXE%" -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Falha ao instalar as dependencias. Verifique sua internet e tente de novo.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

rem ---------- 3. abre o app ----------
".venv\Scripts\python.exe" app.py %*
pause
exit /b 0


rem ================================================================= rotinas

:find_python
rem Procura um Python 3.10+ de verdade (ignora o atalho da Microsoft Store).
set "PYEXE="
if exist ".venv\Scripts\python.exe" (
  set "PYEXE=.venv\Scripts\python.exe"
  exit /b 0
)
for %%C in (python py) do (
  if not defined PYEXE (
    %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=%%C"
  )
)
if defined PYEXE exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
)
if defined PYEXE exit /b 0
exit /b 1

:install_tools
echo.
echo  Faltam algumas ferramentas que o Baixa precisa. Vou instalar agora (so desta vez).
echo  Pode aparecer uma janela do Windows pedindo permissao - clique em Sim.
echo.
where winget >nul 2>nul
if errorlevel 1 (
  echo  O instalador do Windows ^(winget^) nao esta disponivel neste computador.
  echo  Instale manualmente e abra o iniciar.bat de novo:
  echo    Python  : https://www.python.org/downloads/
  echo    ffmpeg  : https://www.gyan.dev/ffmpeg/builds/
  echo    Node.js : https://nodejs.org/
  echo.
  exit /b 1
)
set "WGOPTS=--exact --silent --accept-package-agreements --accept-source-agreements"
call :find_python
if errorlevel 1 (
  echo  - Instalando Python...
  %WINGET% install --id Python.Python.3.12 %WGOPTS%
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo  - Instalando ffmpeg...
  %WINGET% install --id Gyan.FFmpeg %WGOPTS%
)
where node >nul 2>nul
if errorlevel 1 (
  echo  - Instalando Node.js...
  %WINGET% install --id OpenJS.NodeJS.LTS %WGOPTS%
)
rem recarrega o PATH para enxergar o que acabou de ser instalado sem reiniciar a janela
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P"
echo.
echo  Ferramentas prontas.
echo.
exit /b 0
