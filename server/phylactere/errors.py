"""Erreurs métier converties en réponse HTTP par un seul gestionnaire (voir main.py)."""


class ServiceError(Exception):
    """Erreur à renvoyer au client avec ``status_code`` (et ``Retry-After`` si donné)."""

    def __init__(self, message: str, status_code: int, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
