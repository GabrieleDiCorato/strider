import logging
from datetime import date
from dateutil.relativedelta import relativedelta

from src.core.config import get_settings
from src.ingestion.garmin.garmin_auth import build_client, login

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

settings = get_settings()
client = build_client(settings)
print("Logging in...")
login(client, settings)
print("Logged in!")

end_date = date.today()
start_date = end_date - relativedelta(months=1)  # Just 1 month for testing

print(f"Fetching activities from {start_date} to {end_date}...")
try:
    activities = client.get_activities_by_date(start_date.isoformat(), end_date.isoformat())
    print(f"Found {len(activities)} activities.")
except Exception as e:
    print(f"Error: {e}")
