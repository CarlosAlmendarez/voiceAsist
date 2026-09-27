@echo off
rem Crea (o quita, con "quitar") el acceso directo en la carpeta Inicio de Windows
rem para que el asistente arranque solo al iniciar sesion. Es el mismo acceso
rem que activa la opcion "Iniciar con Windows" del icono de la bandeja.
cd /d "%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Asistente de voz.lnk"

if /i "%~1"=="quitar" (
  del "%LNK%" 2>nul
  echo Inicio automatico desactivado.
  exit /b 0
)

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);" ^
  "$s.TargetPath='%~dp0.venv\Scripts\pythonw.exe';" ^
  "$s.Arguments='\"%~dp0asistente.py\"';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Save()"
echo Inicio automatico activado: el asistente arrancara al iniciar sesion.
