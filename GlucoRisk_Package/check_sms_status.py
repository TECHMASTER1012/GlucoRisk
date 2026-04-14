import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()
client = Client(os.environ.get('TWILIO_ACCOUNT_SID'), os.environ.get('TWILIO_AUTH_TOKEN'))
try:
    msg = client.messages('SMfdff9b5ffeeef066be1eb5fd0e4d7556').fetch()
    print("Message Status:", msg.status)
    print("Error code:", msg.error_code)
    print("Error message:", msg.error_message)
except Exception as e:
    print(f"Failed to fetch SMS status: {e}")
