@echo off
REM Run this on a Windows PC with Python 3.10+ installed.
REM Builds TWO programs into the "dist" folder:
REM   SqlBackupUploader.exe       -> real backup, reads config.json
REM   SqlBackupUploader_TEST.exe  -> dummy 30 KB file, no MSSQL, reads config.test.json
REM (the program decides by its own file name: a name ending in _TEST = test mode)

REM Always work in the folder where this .bat file lives
cd /d "%~dp0"

if not exist "sql_backup_uploader.py" (
    echo ERROR: "sql_backup_uploader.py" was not found in %cd%
    goto :err
)

REM config.json / config.test.json are not in Git (they can hold secrets).
REM On a fresh clone, create them from the example and stop so they can be edited first.
set "NEWCFG="
for %%F in (config.json config.test.json) do (
    if not exist "%%F" (
        if not exist "config.example.json" (
            echo ERROR: "%%F" and "config.example.json" were not found in %cd%
            goto :err
        )
        copy /Y config.example.json "%%F" >nul || goto :err
        echo Created %%F from config.example.json
        set "NEWCFG=1"
    )
)
if defined NEWCFG (
    echo.
    echo Edit config.json ^(and config.test.json^) with your own settings, then run this file again.
    pause
    exit /b 1
)

REM Bundle the window icons only if they exist (the program works without them)
set "ICONS="
if exist "SqlBackupUploader.ico" set "ICONS=--icon SqlBackupUploader.ico --add-data SqlBackupUploader.ico;."
if exist "SqlBackupUploader_TEST.ico" set "ICONS=%ICONS% --add-data SqlBackupUploader_TEST.ico;."

py -m pip install -r requirements.txt pyinstaller || goto :err

echo ===== Building PRODUCTION exe =====
py -m PyInstaller --noconfirm --onefile --noconsole --name SqlBackupUploader %ICONS% ^
   --collect-data botocore --hidden-import pyodbc sql_backup_uploader.py || goto :err

echo ===== Building TEST exe =====
py -m PyInstaller --noconfirm --onefile --noconsole --name SqlBackupUploader_TEST %ICONS% ^
   --collect-data botocore --hidden-import pyodbc sql_backup_uploader.py || goto :err

copy /Y config.json      dist\config.json      || goto :err
copy /Y config.test.json dist\config.test.json || goto :err

echo.
echo Done. Files in the "dist" folder:
echo    SqlBackupUploader.exe       + config.json
echo    SqlBackupUploader_TEST.exe  + config.test.json
pause
goto :eof

:err
echo.
echo BUILD FAILED
pause
exit /b 1
