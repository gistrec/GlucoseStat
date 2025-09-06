import os
import time
from datetime import datetime, timedelta, timezone

from pylibrelinkup import APIUrl, PyLibreLinkUp

from database.queries import add_glucose_measurement


FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))

client = PyLibreLinkUp(
    email=os.getenv("EMAIL"),
    password=os.getenv("PASSWORD"),
    api_url=APIUrl.DE,
)
client.authenticate()

patient = client.get_patients()[0]

recent_timestamps: set[datetime] = set()


def _normalize(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def fetch_and_store() -> None:
    global recent_timestamps
    now = datetime.now(timezone.utc)
    six_hours_ago = now - timedelta(hours=6)
    twelve_hours_ago = now - timedelta(hours=12)
    recent_timestamps = {ts for ts in recent_timestamps if ts >= twelve_hours_ago}

    graph_data = client.graph(patient_identifier=patient)
    for measurement in graph_data:
        ts = _normalize(measurement.timestamp)
        if ts >= six_hours_ago and ts not in recent_timestamps:
            add_glucose_measurement(ts, measurement.value)
            recent_timestamps.add(ts)


if __name__ == "__main__":
    while True:
        fetch_and_store()
        time.sleep(FETCH_INTERVAL_MINUTES * 60)

