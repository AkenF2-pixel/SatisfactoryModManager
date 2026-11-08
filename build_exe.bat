@echo off
REM Builds SatisfactoryModManager.exe  (needs Python 3.9+ installed and on PATH)
python -m pip install --upgrade pyinstaller || goto :err
python -m PyInstaller --onefile --windowed --name SatisfactoryModManager satisfactory_mod_manager.py || goto :err
echo.
echo Done! Your exe is in the "dist" folder: dist\SatisfactoryModManager.exe
pause
exit /b 0
:err
echo Build failed - make sure Python is installed and on PATH.
pause
exit /b 1
