@echo off
rem Запуск интерфейса двойным щелчком: поднимает сервер и открывает браузер.
rem Окно закрывать нельзя — в нём и живёт сервер; в нём же идёт лог прогона.
rem
rem Аргументы передаются в serve.py как есть, поэтому можно и так:
rem     manga-tl.bat "out\Глава 1"
rem     manga-tl.bat --port 8767
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Кириллица в логе сервера иначе превращается в мусор: консоль переведена
rem в UTF-8 строкой выше, а Python об этом сам не догадается.
set PYTHONIOENCODING=utf-8

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

echo Остановить — Ctrl+C в этом окне (на вопрос "Terminate batch job" — Y).
echo.
%PY% host\serve.py --open %*
if errorlevel 1 goto failed
endlocal
exit /b 0

:nopython
echo.
echo Python не найден. Он нужен: весь host\ на нём и написан.
echo Поставьте с python.org и при установке отметьте "Add python.exe to PATH".
echo.
pause
exit /b 1

:failed
echo.
echo Сервер не запустился — причина сообщением выше.
echo.
pause
exit /b 1
