@echo off
setlocal
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install --user pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        if "%~1"=="" pause
        exit /b 1
    )
)
echo Building DD_PresetComparator.exe...
python -m PyInstaller --noconfirm --onefile --clean --distpath "%~dp0releases" --workpath "%~dp0build" --specpath "%~dp0build" --name DD_PresetComparator "%~dp0preset_comparator.py"
if errorlevel 1 (
    echo Build failed.
    if "%~1"=="" pause
    exit /b 1
)
if exist "%~dp0build" rmdir /s /q "%~dp0build"
echo Done: "%~dp0releases\DD_PresetComparator.exe"
if "%~1"=="" pause
