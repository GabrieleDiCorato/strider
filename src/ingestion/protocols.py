from datetime import date
from typing import List, Protocol

from src.core.schemas import BronzeLedgerEntry


class SourceConnector(Protocol):
    """
    Protocol for vendor-specific source connectors.
    Responsible for authenticating, discovering entities, and downloading raw payloads to Bronze layer.
    """

    def authenticate(self) -> None:
        """Authenticate with the vendor API."""
        ...

    def fetch_activities(self, user_id: str, start_date: date, end_date: date) -> List[BronzeLedgerEntry]:
        """Fetch raw activity data and save to Bronze."""
        ...

    def fetch_wellness(self, user_id: str, start_date: date, end_date: date) -> List[BronzeLedgerEntry]:
        """Fetch daily wellness data and save to Bronze."""
        ...

    def fetch_workout_definitions(self, user_id: str, start_date: date, end_date: date) -> List[BronzeLedgerEntry]:
        """Fetch raw workout definitions (steps/targets/structure) and save to Bronze."""
        ...

    def fetch_workout_calendar(self, user_id: str, start_date: date, end_date: date) -> List[BronzeLedgerEntry]:
        """Fetch the scheduled workout calendar (JSON-only) and save to Bronze."""
        ...
