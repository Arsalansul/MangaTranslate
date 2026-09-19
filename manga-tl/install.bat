@echo off
rem Установка двойным щелчком: проверяет, что нужные программы на месте, и
rem доставляет всё остальное — шрифты, веса моделей, образ контейнера, ключ.
rem
rem Шрифты вёрстки кладите в папку fonts\ рядом с этим файлом (.otf или .ttf,
rem можно во вложенных папках). Установщик можно запускать сколько угодно раз:
rem он ставит только то, чего ещё нет.
rem
rem     install.bat            установка
rem     install.bat --check    только проверить, ничего не менять
rem     install.bat --yes      не спрашивать, соглашаться на всё
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Кириллица в выводе иначе превращается в мусор: консоль переведена
rem в UTF-8 строкой выше, а Python об этом сам не догадается.
set PYTHONIOENCODING=utf-8

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

%PY% host\install.py %*
set RC=%errorlevel%
echo.
pause
endlocal
exit /b %RC%

:nopython
echo.
echo Python не найден, а установщик сам на нём написан.
echo Поставьте с python.org и при установке отметьте "Add python.exe to PATH".
echo Потом запустите этот файл ещё раз.
echo.
pause
exit /b 1
