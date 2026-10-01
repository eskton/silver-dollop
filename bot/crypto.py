"""Шифрование токенов продавцов перед записью в базу (Fernet, симметричное)."""

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, secret_key: str) -> None:
        try:
            self._fernet = Fernet(secret_key.encode())
        except (ValueError, TypeError) as e:
            raise SystemExit(
                "SECRET_KEY невалиден. Сгенерируй новый: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from e

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("Не удалось расшифровать токен: SECRET_KEY изменился?") from e
