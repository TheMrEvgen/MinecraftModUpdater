from enum import Enum


class ModStatus(str, Enum):
    """
    Статусы модов. Наследование от str позволяет хранить значения прямо в словарях модов,
    сравнивать их со строками и показывать в таблице без дополнительных преобразований.
    """
    NOT_CHECKED = "Не проверено"
    UP_TO_DATE = "Актуально"
    UPDATE_AVAILABLE = "Доступно обновление"
    READY_TO_INSTALL = "Готов к установке"
    NOT_FOUND = "Не найден на Modrinth"
    REQUIRED = "Требуется"
    OPTIONAL = "Опционально"
    INCOMPATIBLE = "⛔ Несовместим"
    VERSION_CONFLICT = "⚠️ Конфликт версий"
    # Статусы процесса установки
    DOWNLOADING = "Скачивание..."
    FINISHING = "Завершение..."
    UPDATED = "Обновлено!"
    INSTALLED = "Установлено!"
    CANCELLED = "Отменено"
    DOWNLOAD_ERROR = "Ошибка!"
    HASH_ERROR = "Ошибка хэша!"
    REPLACE_ERROR = "Ошибка замены!"
    CRITICAL_ERROR = "Крит. ошибка!"

    def __str__(self) -> str:
        return self.value

    @property
    def is_installable(self) -> bool:
        """Статусы, для которых доступно скачивание по кнопкам «Обновить/Установить все»."""
        return self in (ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL)

    @property
    def is_error(self) -> bool:
        return self in (ModStatus.DOWNLOAD_ERROR, ModStatus.HASH_ERROR,
                        ModStatus.REPLACE_ERROR, ModStatus.CRITICAL_ERROR)


INSTALLABLE_STATUSES = (ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL)


class UpdateChannel(str, Enum):
    """Канал обновлений: какие типы версий Modrinth считаются допустимыми."""
    RELEASE = "release"
    BETA = "beta"
    ALPHA = "alpha"

    @property
    def version_types(self) -> list[str]:
        if self is UpdateChannel.RELEASE:
            return ["release"]
        if self is UpdateChannel.BETA:
            return ["release", "beta"]
        return ["release", "beta", "alpha"]

    @property
    def display_name(self) -> str:
        return {
            UpdateChannel.RELEASE: "Только релизы",
            UpdateChannel.BETA: "Релизы и беты",
            UpdateChannel.ALPHA: "Все (включая альфы)",
        }[self]

    @classmethod
    def from_value(cls, value, default: "UpdateChannel | None" = None) -> "UpdateChannel":
        try:
            return cls(value)
        except ValueError:
            return default or cls.RELEASE
