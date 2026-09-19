r"""Ключи моделей — в переменных среды пользователя.

Ключ кладётся туда, откуда его и читает `translate.Engine`: в `os.environ`.
На диске это `HKCU\Environment` — те самые «переменные среды пользователя»
из свойств системы. Не файл в папке проекта: файл уехал бы вместе с папкой
при копировании и попал бы в первый же архив, отправленный «посмотреть».

Модуль отдельный, потому что ключ пишут двое — install.py вопросом в консоли
и serve.py полем в браузере, — а правило «в реестр напрямую, не через setx»
должно быть записано один раз. У setx ключ оказался бы в командной строке
процесса, то есть на виду у диспетчера задач; заодно setx режет значение
на 1024 символах.

Только Windows, как и весь host/: он и так держится на Photoshop через COM.
"""
import ctypes
import os
import re
import winreg

# Куда пишем: ветка пользователя, поэтому прав администратора не нужно.
SUBKEY = "Environment"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

# Имя проверяется здесь, а не только у вызывающего: любая запись в Environment
# — это исполнение кода при следующем входе в систему (PATH, PYTHONSTARTUP),
# и промах в вызывающем не должен стоить так дорого.
NAME = re.compile(r"\A[A-Z][A-Z0-9_]{2,63}\Z")
MAX_LEN = 4096


def _check(name):
    if not NAME.match(name or ""):
        raise ValueError("недопустимое имя переменной: %r" % (name,))
    return name


def _broadcast():
    """Сказать системе, что переменные изменились.

    Новые окна терминала прочитают реестр и так, но проводник и всё, что
    он запускает, держат свою копию блока среды с момента входа в систему.
    Без этого сообщения ключ увидят только те программы, что стартуют
    после перезагрузки.
    """
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 1000, None)


def save(name, value):
    """Записать переменную пользователю и в текущий процесс."""
    _check(name)
    value = (value or "").strip()
    if not value:
        raise ValueError("пустое значение: для удаления есть clear()")
    if len(value) > MAX_LEN:
        raise ValueError("значение длиннее %d символов — это не ключ" % MAX_LEN)
    if any(c in value for c in "\r\n\0"):
        raise ValueError("в значении перенос строки — похоже, скопировалось лишнее")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SUBKEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    _broadcast()
    # Свой процесс блок среды не перечитывает: без этой строки ключ заработал
    # бы только после перезапуска serve.py.
    os.environ[name] = value


def clear(name):
    """Убрать переменную. Нет её — и хорошо, это не ошибка."""
    _check(name)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SUBKEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass
    else:
        _broadcast()
    os.environ.pop(name, None)


def stored(name):
    """Лежит ли значение в реестре — то есть переживёт ли перезапуск.

    Отличается от `os.environ`: переменную могли задать `set`'ом в том окне,
    из которого запущен сервер. Тогда ключ работает, но только здесь и
    только до закрытия окна, и человеку про это честнее сказать.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SUBKEY) as k:
            value, _ = winreg.QueryValueEx(k, name)
    except (OSError, ValueError):
        return False
    return bool(str(value).strip())
