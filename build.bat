@echo off
setlocal
rem ASCII only in this file: cmd.exe parses batch files with the local ANSI
rem codepage (CP949 on Korean Windows), and any multibyte character shifts
rem the parser's byte offsets and corrupts the commands that follow.
rem Run from the folder this script lives in, so double-clicking works.
cd /d "%~dp0"

echo === YoutubeLiveNotion Windows build ===

rem Refuse to build while the app is running - it locks files in dist\ and
rem PyInstaller fails halfway with a PermissionError when clearing the folder.
tasklist /FI "IMAGENAME eq YoutubeLiveNotion.exe" | find /i "YoutubeLiveNotion.exe" >nul
if not errorlevel 1 (
    echo [ERROR] YoutubeLiveNotion.exe is currently running.
    echo Close the app first, then run this script again.
    pause
    exit /b 1
)

rem A .venv copied from another OS (e.g. macOS) has no Scripts\python.exe and
rem silently breaks activation below - recreate it as a real Windows venv.
if exist .venv (
    if not exist .venv\Scripts\python.exe (
        echo Existing .venv is not a Windows venv. Recreating it...
        rmdir /s /q .venv
    )
)
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

pip install --upgrade pip
rem requirements.txt includes the NVIDIA cuBLAS/cuDNN wheels on Windows
rem (about 1GB download on the first build) so transcription can use the GPU.
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check the pip output above.
    pause
    exit /b 1
)
pip install pyinstaller

rem PyInstaller wipes the dist folder on rebuild, taking the downloaded Whisper
rem model cache (models\, ~1.5GB) with it and forcing a re-download on the next
rem run. Stash it in the project root and restore it after the build. If the
rem build fails the stash stays behind and the next successful build restores it.
if exist "dist\YoutubeLiveNotion\models" (
    if exist "models_backup_tmp" rmdir /s /q "models_backup_tmp"
    move "dist\YoutubeLiveNotion\models" "models_backup_tmp" >nul
)

pyinstaller youtube-live-notion.spec --noconfirm
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. Check the output above.
    pause
    exit /b 1
)

set DIST_DIR=dist\YoutubeLiveNotion

if exist "models_backup_tmp" (
    move "models_backup_tmp" "dist\YoutubeLiveNotion\models" >nul
)

rem NOTE: no set-then-use of variables inside this if-block - %VAR% expands at
rem parse time for the whole block (delayed expansion is off), so literals are used.
if not exist "%DIST_DIR%\ffmpeg.exe" (
    echo.
    echo === Bundling ffmpeg ^(first build only, downloads about 80MB^)... ===
    powershell -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'dist\YoutubeLiveNotion\ffmpeg_download.tmp.zip'"
    powershell -Command "Expand-Archive -Path 'dist\YoutubeLiveNotion\ffmpeg_download.tmp.zip' -DestinationPath 'dist\YoutubeLiveNotion\ffmpeg_extract_tmp' -Force"
    for /f "delims=" %%D in ('dir /b /ad "dist\YoutubeLiveNotion\ffmpeg_extract_tmp\ffmpeg-*"') do (
        copy /y "dist\YoutubeLiveNotion\ffmpeg_extract_tmp\%%D\bin\ffmpeg.exe" "dist\YoutubeLiveNotion\ffmpeg.exe"
    )
    rmdir /s /q "dist\YoutubeLiveNotion\ffmpeg_extract_tmp"
    del /q "dist\YoutubeLiveNotion\ffmpeg_download.tmp.zip"
) else (
    echo ffmpeg.exe already exists in dist folder. Skipping download.
)

rem deno is the JS runtime yt-dlp needs for full-quality VOD downloads
rem (web_embedded client n-challenge). Optional: without it the app still
rem works but VOD downloads fall back to 360p.
if not exist "%DIST_DIR%\deno.exe" (
    echo.
    echo === Bundling deno ^(first build only, downloads about 40MB^)... ===
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile 'dist\YoutubeLiveNotion\deno_download.tmp.zip'"
    powershell -Command "Expand-Archive -Path 'dist\YoutubeLiveNotion\deno_download.tmp.zip' -DestinationPath 'dist\YoutubeLiveNotion\deno_extract_tmp' -Force"
    if exist "dist\YoutubeLiveNotion\deno_extract_tmp\deno.exe" (
        copy /y "dist\YoutubeLiveNotion\deno_extract_tmp\deno.exe" "dist\YoutubeLiveNotion\deno.exe"
    ) else (
        echo [WARN] deno.exe was not downloaded. VOD downloads will be limited to 360p.
    )
    if exist "dist\YoutubeLiveNotion\deno_extract_tmp" rmdir /s /q "dist\YoutubeLiveNotion\deno_extract_tmp"
    if exist "dist\YoutubeLiveNotion\deno_download.tmp.zip" del /q "dist\YoutubeLiveNotion\deno_download.tmp.zip"
) else (
    echo deno.exe already exists in dist folder. Skipping download.
)

if exist config.json.example (
    copy /y config.json.example "%DIST_DIR%\config.json.example"
)

rem PyInstaller wipes the dist folder on rebuild, taking the user's filled-in
rem config.json with it - restore it from the project root copy if we have one.
if exist config.json (
    copy /y config.json "%DIST_DIR%\config.json"
)

rem The claude_code summarizer backend needs the Claude Code CLI installed
rem and logged in separately - this is optional and not auto-installed here.
if not exist "%USERPROFILE%\.local\bin\claude.exe" (
    where claude >nul 2>nul
    if errorlevel 1 (
        echo.
        echo === NOTE: Claude Code CLI not found ===
        echo The claude_code summarizer backend requires the Claude Code CLI.
        echo Install it with: irm https://claude.ai/install.ps1 ^| iex
        echo Then log in once by running claude, or use the app's Claude Login button.
    )
)

echo.
echo === Build complete ===
echo The dist\YoutubeLiveNotion folder now contains the executable and ffmpeg.exe.
echo Copy this whole folder to run it on another PC without any separate install.
echo ^(Copy config.json.example to config.json and fill in your actual key values.^)
echo.

endlocal
pause
