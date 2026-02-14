@echo off
setlocal
cd /d %~dp0..
python -m python_meowfi.gui
endlocal
