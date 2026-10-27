@echo off
rem Starts PhoneMic; the window shows the address and the pairing code.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m phonemic %*
pause
