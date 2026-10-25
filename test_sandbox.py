"""
Upstox sandbox smoke test: place a LIMIT order, then cancel it.

Setup:
    pip install requests
    export UPSTOX_SANDBOX_TOKEN="<token from the sandbox app>"     # Mac/Linux
    setx UPSTOX_SANDBOX_TOKEN "<token>"                            # Windows (reopen terminal)

Run:
    python test_sandbox.py
"""
import json
import os
import sys

import requests

# BASE_URL = "https://sandbox.upstox.com"  # sandbox host, NOT api.upstox.com / api-hft.upstox.com
BASE_URL = "https://api-sandbox.upstox.com"  # sandbox host, NOT api.upstox.com / api-hft.upstox.com
TOKEN = os.environ.get("UPSTOX_SANDBOX_TOKEN")

if not TOKEN:
    sys.exit("Set the UPSTOX_SANDBOX_TOKEN environment variable first.")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": f"Bearer {TOKEN}",
}

# Reliance Industries on NSE. Format is NSE_EQ|<ISIN>.
# Other instrument keys come from Upstox's instruments file.
ORDER = {
    "quantity": 1,
    "product": "I",             # I = intraday, D = delivery
    "validity": "DAY",
    "price": 1000.0,            # a LIMIT order needs a price; adjust if rejected
    "tag": "sandbox-test",
    "instrument_token": "NSE_EQ|INE002A01018",
    "order_type": "LIMIT",      # market orders are not allowed for API trading
    "transaction_type": "BUY",
    "disclosed_quantity": 0,
    "trigger_price": 0,
    "is_amo": False,
}


def show(label, resp):
    print(f"\n=== {label} ===")
    print("HTTP status:", resp.status_code)
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)


# 1) Place order
r = requests.post(f"{BASE_URL}/v2/order/place", headers=HEADERS, json=ORDER, timeout=15)
show("PLACE ORDER", r)
if r.status_code != 200:
    sys.exit("\nPlace order failed. See the hints in the chat for what the status code means.")

data = r.json().get("data", {})
order_id = data.get("order_id") or (data.get("order_ids") or [None])[0]
if not order_id:
    sys.exit("\nNo order id in the response, so there is nothing to cancel.")
print("\nOrder id:", order_id)

# 2) Cancel the same order
r = requests.delete(
    f"{BASE_URL}/v2/order/cancel",
    headers=HEADERS,
    params={"order_id": order_id},
    timeout=15,
)
show("CANCEL ORDER", r)

print("\nIf both steps returned status 'success', your sandbox setup works.")