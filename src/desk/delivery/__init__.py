"""Strategy-agnostic signal delivery.

This package posts a signal packet, stores it until that post succeeds, and
records a reviewer decision. It does not import setup detectors. A new setup
is eligible when the scanner emits a packet whose hard gates all pass. A
master_decision cannot clear a failed gate.
"""
