#!/usr/bin/env python3
"""List phone numbers available in the configured Twilio account."""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


def main():
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        print("Error: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN environment variables are required")
        sys.exit(1)

    from twilio.rest import Client
    client = Client(account_sid, auth_token)

    numbers = client.incoming_phone_numbers.list()

    if not numbers:
        print("No phone numbers found in this account.")
        return

    print(f"Found {len(numbers)} phone number(s):\n")
    for n in numbers:
        capabilities = []
        if n.capabilities.get("voice"):
            capabilities.append("voice")
        if n.capabilities.get("sms"):
            capabilities.append("sms")
        if n.capabilities.get("mms"):
            capabilities.append("mms")

        friendly = f"  ({n.friendly_name})" if n.friendly_name != n.phone_number else ""
        print(f"  {n.phone_number}{friendly}  [{', '.join(capabilities)}]")


if __name__ == "__main__":
    main()
