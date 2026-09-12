# Contributing

This is an experimental local control plane. Submit changes against explicit acceptance tests. Do not add credentials, personal account observations, raw provider logs or private task payloads to issues or pull requests.

Use Python 3.12. Install `pip install -e '.[test]'`, then `pytest -q`. The optional real-service tests require a disposable PostgreSQL database and RabbitMQ instance; never point them at production. `GRID_RUN_BROKER_TEST=1` enables worker-kill tests. `GRID_TEST_BROKER_RESTART` is a JSON argv array for an operator-supplied restart command targeting ONLY the disposable broker. Without it, restart tests skip.

Do not describe synthetic, mocked or skipped tests as real provider/service validation. Attach a sanitized reproduction and record which tests actually ran. A passed receipt check is not independent review or proof of provider identity.

Supported local platforms: macOS and Linux. Windows process supervision is not implemented. Database credentials and trusted adapter commands remain local configuration. No automatic publication or merge is part of this alpha.
