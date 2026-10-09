"""NexaBank core banking API (stdlib only): the tools the AI agent wants to use.

It contains NO policy code and is only reachable through Envoy (it sits on the private `core`
network). If a request arrives here, Envoy + OPA already allowed it.

  POST /tools/lookup_customer   {"customer_id"}
  POST /tools/issue_refund      {"customer_id", "amount", "reason"}
  POST /tools/transfer_money    {"from_customer_id", "to_account", "amount", "purpose"}
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ponytail: in-memory demo data, restart the bank-api container to reset balances.
CUSTOMERS = {
    "C-1001": {"customer_id": "C-1001", "name": "Priya Sharma", "phone": "+91-98200-11223",
               "email": "priya.sharma@example.in", "pan": "ABCPS1234K", "account_number": "NXB-00012345",
               "balance": 2500000, "known_payees": ["NXB-00077777"]},
    "C-1002": {"customer_id": "C-1002", "name": "Arjun Nair", "phone": "+91-99000-44556",
               "email": "arjun.nair@example.in", "pan": "BKNPA5678L", "account_number": "NXB-00054321",
               "balance": 1800000, "known_payees": ["NXB-00088888"]},
}
LEDGER = []


def lookup_customer(customer_id):
    c = CUSTOMERS.get(customer_id)
    if not c:
        return {"status": "error", "error": f"customer {customer_id} not found"}
    return {k: v for k, v in c.items() if k != "known_payees"}


def issue_refund(customer_id, amount, reason=""):
    c = CUSTOMERS.get(customer_id)
    if not c:
        return {"status": "error", "error": f"customer {customer_id} not found"}
    c["balance"] += amount
    LEDGER.append(tx := {"transaction_id": f"RF-{len(LEDGER) + 1:04d}", "type": "refund", "customer_id": customer_id,
                         "amount": amount, "reason": reason})
    return {"status": "executed", **tx}


def transfer_money(from_customer_id, to_account, amount, purpose=""):
    c = CUSTOMERS.get(from_customer_id)
    if not c:
        return {"status": "error", "error": f"customer {from_customer_id} not found"}
    if amount > c["balance"]:  # business validation, not policy: the money simply isn't there
        return {"status": "error", "error": "insufficient balance"}
    c["balance"] -= amount
    LEDGER.append(tx := {"transaction_id": f"TX-{len(LEDGER) + 1:04d}", "type": "transfer", "from": from_customer_id,
                         "to_account": to_account, "amount": amount, "purpose": purpose})
    return {"status": "executed", **tx}


TOOLS = {"lookup_customer": lookup_customer, "issue_refund": issue_refund, "transfer_money": transfer_money}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        name = self.path.removeprefix("/tools/")
        try:
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            result = TOOLS[name](**args) if name in TOOLS else {"status": "error", "error": f"unknown tool {name}"}
        except (ValueError, TypeError) as e:  # bad JSON or wrong/missing arguments
            result = {"status": "error", "error": str(e)}
        print(time.strftime("%H:%M:%S"), f"executed {name} -> {result.get('status', 'ok')}", flush=True)
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("NexaBank core banking API on :7000 (reachable only through Envoy)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 7000), Handler).serve_forever()
