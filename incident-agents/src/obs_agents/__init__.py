"""Actor-model agent system for Observability incident response.

A POC (SIP-1765) using Pykka actors that monitor the Observability stack
(Prometheus, Loki, Tempo) in parallel, detect anomalies, and coordinate
incident response through message passing — no shared mutable state.
"""

__version__ = "0.1.0"
