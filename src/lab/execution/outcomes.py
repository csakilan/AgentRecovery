"""Fixed observations for execution evidence that has no original HTTP response."""

TRANSPORT_UNCERTAIN = "Payment outcome is unknown because no response was received."
INTENT_CONFLICT = "Not executed: this run already has a payment operation with different arguments."
STATUS_CONFIRMED = "Payment status confirmed that the recorded payment succeeded."
STATUS_CONFLICT = (
    "Payment status conflicts with the recorded payment arguments; success is not confirmed."
)
RECOVERY_UNRESOLVED = "Payment outcome remains unresolved at the recovery deadline."
