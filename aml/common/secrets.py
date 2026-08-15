# common/secrets.py
"""
Единая точка чтения API-ключей и прочих секретов платформы.

Поддерживает два источника, в порядке приоритета:
1. Docker secrets: переменная `<NAME>_FILE` содержит путь к файлу с секретом
   (стандартный конвенция для docker-compose secrets / Kubernetes secrets).
2. Обычная переменная окружения `<NAME>` (например, из .env через env_file
   в docker-compose, или через `export` в терминале).

Использование во всех клиентах (evm_adapter, будущие btc_adapter/tron_adapter
и т.д.) должно идти только через эту функцию, чтобы не дублировать логику
чтения секретов в каждом клиенте по отдельности.
"""

import os
from typing import Optional

from dotenv import load_dotenv, find_dotenv

# Подхватываем .env автоматически при первом импорте этого модуля — ищем файл
# вверх по дереву каталогов от текущей рабочей директории, так что работает
# и при запуске из корня проекта, и из вложенных папок (scripts/, agent/ и т.д.)
load_dotenv(find_dotenv(usecwd=True))


class SecretNotFoundError(Exception):
    """Секрет обязателен, но не найден ни в _FILE, ни в обычной переменной окружения."""
    pass


def get_secret(name: str, required: bool = True) -> Optional[str]:
    """
    Возвращает значение секрета `name`.

    Args:
        name: Имя секрета/переменной окружения, например "BLOCKSCOUT_API_KEY".
        required: Если True и секрет не найден — выбрасывает SecretNotFoundError.
                  Если False — возвращает None при отсутствии.

    Порядок поиска:
        1. `{name}_FILE` — путь к файлу (Docker secrets). Значение — содержимое
           файла с обрезанными пробелами/переводами строк по краям.
        2. `{name}` — значение переменной окружения напрямую.
    """
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError as e:
            raise SecretNotFoundError(
                f"{name}_FILE указывает на '{file_path}', но файл не читается: {e}"
            ) from e

    value = os.environ.get(name)
    if value:
        return value

    if required:
        raise SecretNotFoundError(
            f"Секрет '{name}' не найден. Задайте переменную окружения '{name}' "
            f"(например, через .env) или '{name}_FILE' (путь к Docker secret)."
        )
    return None
