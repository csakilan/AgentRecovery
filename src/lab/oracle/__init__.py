"""Independent evaluator. Reads the ledger; never trusts what the agent says it did."""

# Written into `results.evaluator_version` beside every verdict. Bump it on any change to
# what the evaluator reads or how it decides (lab.oracle.ledger, lab.oracle.score), so a
# stored verdict says which evaluator produced it. Defined here, once.
EVALUATOR_VERSION = "oracle-v1"
