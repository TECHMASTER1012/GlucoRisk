import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
from_num = os.environ.get('TWILIO_FROM_NUMBER')
to_num = os.environ.get('TWILIO_TO_NUMBER')

try:
    print(f"Sending from {from_num} to {to_num}...")
    client = Client(account_sid, auth_token)
    message = client.messages.create(
        body="This is a test message from GlucoRisk.",
        from_=from_num,
        to=to_num
    )
    print(f"Message SID: {message.sid}")
    print(f"Message Status: {message.status}")
    if message.error_message:
        print(f"Error Message: {message.error_message}")
        print(f"Error Code: {message.error_code}")
except Exception as e:
    print(f"Exception encountered: {e}")
