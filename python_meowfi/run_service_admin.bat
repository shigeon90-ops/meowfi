@echo off
setlocal
cd /d %~dp0..
set ROOT=%CD%
rem PowerShell does not support `cd /d` syntax from cmd.exe, use Set-Location.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command \"Set-Location -LiteralPath ''%ROOT%''; python -m python_meowfi.service\"'"
endlocal
