"""End-to-end check against the compose stack.

A lost acknowledgement must commit, return an error, and be revealed by status.
"""

import sys
import uuid

import httpx

PUBLIC = "http://localhost:8100"
INTERNAL = "http://localhost:8101"
CLIENT = {"Authorization": "Bearer dev-client-token"}
CONTROL = {"Authorization": "Bearer dev-control-token"}


def main() -> int:
    ns = f"smoke-{uuid.uuid4().hex[:8]}"
    httpx.put(
        f"{INTERNAL}/internal/namespaces/{ns}",
        headers=CONTROL,
        json={"profile": "controlled", "fault_plan": {"charge": {"1": "commit_then_error"}}},
    ).raise_for_status()
    headers = {**CLIENT, "X-Lab-Namespace": ns}
    body = {"amount": 49.99, "idempotency_key": "smoke-1", "order_id": "1234", "currency": "USD"}
    first = httpx.post(f"{PUBLIC}/v1/charges", headers=headers, json=body)
    status = httpx.get(f"{PUBLIC}/v1/payments/smoke-1", headers=headers)
    print("charge:", first.status_code, first.json())
    print("status:", status.status_code, status.json())
    httpx.delete(f"{INTERNAL}/internal/namespaces/{ns}", headers=CONTROL)
    ok = first.status_code == 504 and status.json().get("status") == "SUCCEEDED"
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
