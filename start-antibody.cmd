@echo off
rem Double-click to set up and start ANTIBODY (Windows).
rem PowerShell 7 windows leave module paths that break Windows PowerShell; let it use its own
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-antibody.ps1"
if errorlevel 1 (
  echo.
  echo ANTIBODY stopped with an error. The messages above say why.
  pause
)
