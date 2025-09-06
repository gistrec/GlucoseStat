import os

from pylibrelinkup import PyLibreLinkUp, APIUrl


client = PyLibreLinkUp(
    email=os.getenv("EMAIL"),
    password=os.getenv("PASSWORD"),
    api_url=APIUrl.DE
)
client.authenticate()

patient = client.get_patients()[0]

graph_data = client.graph(patient_identifier=patient)

for measurement in graph_data:
    print(f"{measurement.value} {measurement.timestamp} {measurement.factory_timestamp}")

