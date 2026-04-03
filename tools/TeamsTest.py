"""
test_teams.py — Test Teams webhook with Adaptive Card format
Run: python test_teams.py
"""
import requests
import os
from dotenv import load_dotenv
load_dotenv()

url = os.getenv("TEAMS_WEBHOOK_URL", "")
if not url:
    print("ERROR: TEAMS_WEBHOOK_URL not set in .env")
    exit(1)

print(f"URL type: {'Power Platform' if 'powerplatform.com' in url else 'Other'}")
print(f"Sending Adaptive Card to Teams...")

# Adaptive Card format — what "Send webhook alerts to a channel" expects
payload = {
    "type": "message",
    "attachments": [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type":    "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type":   "TextBlock",
                        "text":   "IBM AIOps — Teams Integration Test",
                        "weight": "Bolder",
                        "size":   "Medium",
                        "color":  "Accent"
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": "Status",  "value": "Connection verified"},
                            {"title": "Channel", "value": "AIOps_Incident_Resolution"},
                            {"title": "Source",  "value": "AIOps Multi-Agent System"},
                        ]
                    },
                    {
                        "type": "TextBlock",
                        "text": "Teams integration is working. AIOps alerts will appear here.",
                        "wrap": True,
                        "size": "Small"
                    }
                ]
            }
        }
    ]
}

try:
    resp = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15
    )
    print(f"Status code : {resp.status_code}")
    print(f"Response    : {resp.text[:200]}")
    if resp.status_code in (200, 202, 204):
        print("Result: SUCCESS — check your Teams channel")
    else:
        print("Result: FAILED — see error above")
except Exception as exc:
    print(f"Connection error: {exc}")