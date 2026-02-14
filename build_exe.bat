@echo off
setlocal
cd /d "%~dp0"

set ADDDATA=
set ICONARG=
if exist "assets\logo.png" (
  set ADDDATA=--add-data "assets\logo.png;assets"
  echo [MeowFi] Bundling assets\logo.png into EXE
  if not exist "assets\logo.ico" (
    echo [MeowFi] Converting assets\logo.png -> assets\logo.ico
    python -c "from PIL import Image; im=Image.open('assets/logo.png').convert('RGBA'); im.save('assets/logo.ico', format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
  )
) else (
  echo [MeowFi] assets\logo.png not found, building without bundled logo
)

if exist "assets\logo.ico" (
  set ICONARG=--icon "assets\logo.ico"
  echo [MeowFi] Using EXE icon: assets\logo.ico
) else (
  echo [MeowFi] assets\logo.ico not found, using default icon
)

pyinstaller --noconfirm --clean --onefile --windowed --name MeowFi ^
  --paths . ^
  --hidden-import python_meowfi.service ^
  --hidden-import python_meowfi.client ^
  --hidden-import python_meowfi.common ^
  --hidden-import python_meowfi.backend ^
  --hidden-import python_meowfi.dhcp_fallback ^
  %ICONARG% ^
  %ADDDATA% ^
  python_meowfi\gui.py

if errorlevel 1 (
  echo [MeowFi] Build failed.
  exit /b 1
)

echo [MeowFi] Build complete: dist\MeowFi.exe
endlocal
