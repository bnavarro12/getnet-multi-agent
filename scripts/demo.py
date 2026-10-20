"""Send the challenge's example scenarios to the running API and print a readable summary.

    python scripts/demo.py                    # against http://localhost:8000
    docker compose exec api python scripts/demo.py
"""

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

SCENARIOS = [
    "What's the difference between the Get Clássica and the Get Smart?",
    "What's the weather forecast in Porto Alegre tomorrow?",
    "When will the money from yesterday's sales be deposited?",
    "Do I need a bank account to receive my sales via Pix?",
    "My card machine won't connect to the internet, what should I do?",
    "How does receivables advance (antecipação) work with Getnet?",
    "What's the euro exchange rate today?",
    "My card machine is showing a transaction decline error.",
    "How many installments can I split a sale into with the crediário?",
    "Can I sell through WhatsApp using the Payment Link?",
]


def post(message: str, user_id: str = "cliente1988") -> dict:
    req = urllib.request.Request(f"{BASE}/chat", data=json.dumps({"message": message, "user_id": user_id}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


for i, msg in enumerate(SCENARIOS, 1):
    res = post(msg)
    print("=" * 90)
    print(f"[{i}] {msg}")
    print(f"    agent={res['agent']}  route={res['route'].get('route')}  conf={res['route'].get('confidence')}  "
          f"tools={res['tools_used']}  escalated={res['escalated']}  {res['trace']['total_ms']:.0f}ms")
    print(f"\n{res['response']}\n")
    for s in res["sources"][:3]:
        print(f"    source: {s['url']}")
    time.sleep(8)  # stay under free-tier rate limits
