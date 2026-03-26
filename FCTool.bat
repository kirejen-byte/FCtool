@echo off
title FCTool - Fleet Commander Assistant
cd /d "%~dp0"
python fc_gui.py
if errorlevel 1 (
    echo.
    echo FCTool exited with an error. Press any key to close.
    pause >nul
)
