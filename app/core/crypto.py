"""Secrets the database has to hold in a form that can be read back, such as the sign-in to
someone's calendar, are encrypted with a key that only the environment holds (ADR 0036).

A token this app only checks is stored as a hash (`app/core/identity.py`). This is for the few it
must present again to someone else.
"""
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class Unreadable(Exception):
    """A stored secret that none of the configured keys opens: the key was changed or lost."""


def new_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(keys: str) -> MultiFernet:
    """`keys` is one key, or several with commas between: the first encrypts, every one decrypts,
    so a key is replaced by putting the new one in front."""
    return MultiFernet([Fernet(key.strip().encode()) for key in keys.split(",") if key.strip()])


def check_keys(keys: str) -> None:
    """Raises ValueError unless every key is a Fernet key (32 random bytes, url-safe base64)."""
    try:
        _fernet(keys)
    except ValueError as exc:
        raise ValueError(
            "CALENDAR_SECRET_KEY must be a Fernet key; make one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`"
        ) from exc


def seal(keys: str, secret: str) -> str:
    return _fernet(keys).encrypt(secret.encode()).decode()


def unseal(keys: str, sealed: str) -> str:
    try:
        return _fernet(keys).decrypt(sealed.encode()).decode()
    except InvalidToken:
        raise Unreadable from None
