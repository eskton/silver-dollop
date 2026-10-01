class PlayerokError(Exception):
    """Любая ошибка при обращении к Playerok."""


class AuthRequired(PlayerokError):
    """Сессия невалидна или истекла: продавцу нужно войти заново."""
