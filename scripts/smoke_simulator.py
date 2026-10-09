"""End-to-end check against the compose stack.

A lost acknowledgement must commit exactly once, return an error, and be revealed by
status with the right amount. A second charge under the same key must then replay that
one effect instead of creating another.

Only the public API and the internal control API are used; the database is never opened.
"""

import sys
import uuid

import httpx

PUBLIC = "http://localhost:8100"
INTERNAL = "http://localhost:8101"
CLIENT = {"Authorization": "Bearer dev-client-token"}
CONTROL = {"Authorization": "Bearer dev-control-token"}

EXPECTED_AMOUNT = "49.99"
EXPECTED_ORDER = "1234"
EXPECTED_CURRENCY = "USD"
KEY = "smoke-1"


def main() -> int:
    ns = f"smoke-{uuid.uuid4().hex[:8]}"
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        print(("ok     " if condition else "FAILED ") + message)
        if not condition:
            failures.append(message)

    try:
        httpx.put(
            f"{INTERNAL}/internal/namespaces/{ns}",
            headers=CONTROL,
            json={"profile": "controlled", "fault_plan": {"charge": {"1": "commit_then_error"}}},
        ).raise_for_status()
        headers = {**CLIENT, "X-Lab-Namespace": ns}
        body = {
            "amount": float(EXPECTED_AMOUNT),
            "idempotency_key": KEY,
            "order_id": EXPECTED_ORDER,
            "currency": EXPECTED_CURRENCY,
        }

        # Call 1 commits and then loses the acknowledgement.
        first = httpx.post(f"{PUBLIC}/v1/charges", headers=headers, json=body)
        print("charge:", first.status_code, first.text)
        check(first.status_code == 504, f"first charge is 504 (got {first.status_code})")

        # Status must reveal the committed effect, with the right values.
        status = httpx.get(f"{PUBLIC}/v1/payments/{KEY}", headers=headers)
        print("status:", status.status_code, status.text)
        check(status.status_code == 200, f"status call is 200 (got {status.status_code})")
        status_body = status.json()
        check(
            status_body.get("status") == "SUCCEEDED",
            f"status is SUCCEEDED (got {status_body.get('status')!r})",
        )
        charge = status_body.get("charge") or {}
        check(
            charge.get("amount") == EXPECTED_AMOUNT,
            f"committed amount is {EXPECTED_AMOUNT} (got {charge.get('amount')!r})",
        )
        check(
            charge.get("order_id") == EXPECTED_ORDER,
            f"committed order is {EXPECTED_ORDER} (got {charge.get('order_id')!r})",
        )
        check(
            charge.get("currency") == EXPECTED_CURRENCY,
            f"committed currency is {EXPECTED_CURRENCY} (got {charge.get('currency')!r})",
        )

        # Same key again, no fault scheduled for call 2. If the first call committed once,
        # this must replay that very charge. A second commit under the key would show up as
        # a new charge id or as replayed=false.
        replay = httpx.post(f"{PUBLIC}/v1/charges", headers=headers, json=body)
        print("replay:", replay.status_code, replay.text)
        check(replay.status_code == 200, f"replay charge is 200 (got {replay.status_code})")
        replay_body = replay.json()
        check(
            replay_body.get("replayed") is True,
            f"replay reports replayed=true (got {replay_body.get('replayed')!r})",
        )
        replay_charge = replay_body.get("charge") or {}
        check(
            charge.get("charge_id") is not None
            and replay_charge.get("charge_id") == charge.get("charge_id"),
            "replay returns the same charge_id as status, so exactly one charge exists "
            f"(status {charge.get('charge_id')!r}, replay {replay_charge.get('charge_id')!r})",
        )
    finally:
        # Always remove the namespace, including after a failed request or assertion.
        httpx.delete(f"{INTERNAL}/internal/namespaces/{ns}", headers=CONTROL)

    print("OK" if not failures else f"FAILED ({len(failures)} checks)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
