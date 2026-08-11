@echo off
title FCTool - Fleet Commander Assistant
cd /d "%~dp0"
rem Resolve the interpreter explicitly: a toolchain prepended to PATH can
rem shadow `python` with a venv missing FCTool's deps (2026-08-11: a 3.11
rem agent venv without pygame hijacked the double-click launch). Prefer the
rem standard per-user python.org installs - 3.12 (daily driver) then 3.13 -
rem and fall back to PATH `python` so other setups behave exactly as before.
set "FCPY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%FCPY%" set "FCPY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%FCPY%" set "FCPY=python"
"%FCPY%" fc_gui.py
if errorlevel 1 (
    echo.
    echo FCTool exited with an error. Press any key to close.
    pause >nul
)
