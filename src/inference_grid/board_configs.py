"""Board config ordering shared by the operator digest and the local dashboard.

Reads each ``*.json`` in ``boards_dir`` (a board-tick config, optionally with a
``priority``) and orders them by that priority then name. The sequential, one-shared-
ledger `tick-all` runner that used to live here is gone: `deployments/local/tick_boards.py`
ticks every board on its own thread and is the runner in production.
"""

import json
from pathlib import Path


def ordered_configs(boards_dir):
    """The board configs in boards_dir, ordered by declared priority then name."""
    configs = []
    for path in sorted(Path(boards_dir).glob("*.json")):
        config = json.loads(path.read_text())
        configs.append((config.get("priority", 100), path.name, config))
    return [config for _, _, config in sorted(configs, key=lambda entry: (entry[0], entry[1]))]
