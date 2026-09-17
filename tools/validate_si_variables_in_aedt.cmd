@echo off
setlocal
C:\ProgramData\anaconda3\python.exe "%~dp0validate_si_variables_in_aedt.py"
if errorlevel 1 pause
