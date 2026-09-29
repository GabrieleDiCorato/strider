import logging
from datetime import date
from dateutil.relativedelta import relativedelta

from src.core.config import get_settings
from src.core.schemas import EntityType
from src.ingestion.garmin.garmin_connector import GarminConnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

def main():
    settings = get_settings()
    logger.info(f"Loaded settings for user_id={settings.user_id}")

    connector = GarminConnector(settings=settings)
    
    end_date = date.today()
    start_date = end_date - relativedelta(months=6)

    logger.info(f"Downloading data from {start_date} to {end_date}")

    for entity_type in EntityType:
        logger.info(f"Fetching entity type: {entity_type.value} - This may take a while due to rate limits...")
        try:
            entries = connector.fetch(
                user_id=settings.user_id,
                entity_type=entity_type,
                start=start_date,
                end=end_date
            )
            logger.info(f"Successfully fetched {len(entries)} entries for {entity_type.value}")
        except Exception as e:
            logger.error(f"Failed to fetch {entity_type.value}: {e}", exc_info=True)

if __name__ == "__main__":
    main()
