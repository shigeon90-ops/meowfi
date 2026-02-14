@echo off
setlocal
cd /d "%~dp0"

set ADDDATA=
if exist "assets\logo.png" (
  set ADDDATA=--add-data "assets\logo.png;assets"
  echo [MeowFi] Bundling assets\logo.png into EXE
) else (
  echo [MeowFi] assets\logo.png not found, building without bundled logo
)

pyinstaller --noconfirm --clean --onefile --windowed --name MeowFi ^
  --paths . ^
  --hidden-import python_meowfi.service ^
  --hidden-import python_meowfi.client ^
  --hidden-import python_meowfi.common ^
  --hidden-import python_meowfi.backend ^
  --hidden-import python_meowfi.dhcp_fallback ^
  %ADDDATA% ^
  python_meowfi\gui.py

if errorlevel 1 (
  echo [MeowFi] Build failed.
  exit /b 1
)

echo [MeowFi] Build complete: dist\MeowFi.exe
endlocal
