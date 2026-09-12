"""Provider lane adapters: one module per native interface, configured from a private lanes.json.

Credentials, executables and plan units never live in this package; validate_lane_config
checks the operator's private file and each lane module reads only what it needs from it.
"""
