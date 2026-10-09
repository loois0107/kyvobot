@echo off
venv\Scripts\python.exe -m PyInstaller --onefile --noconsole --name DorCompanion main.py
echo.
echo dist\DorCompanion.exe
