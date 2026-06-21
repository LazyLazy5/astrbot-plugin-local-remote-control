@echo off
chcp 65001 >nul
if "%NAPCAT_DIR%"=="" (
  echo Please set NAPCAT_DIR to your NapCat directory.
  echo Example: set "NAPCAT_DIR=C:\path\to\NapCat"
  exit /b 1
)
if not exist "%NAPCAT_DIR%\NapCatWinBootMain.exe" (
  echo NapCatWinBootMain.exe not found in "%NAPCAT_DIR%".
  exit /b 1
)
cd /d "%NAPCAT_DIR%"
".\NapCatWinBootMain.exe"
pause
