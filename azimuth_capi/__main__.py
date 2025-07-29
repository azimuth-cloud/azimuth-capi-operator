import asyncio

import kopf

from . import metrics


async def main():
    """
    Run the operator and the metrics server together.
    """
    # This import is required to pick up the operator handlers
    from . import operator  # noqa: F401

    kopf.configure(log_prefix=True, verbose=True)
    tasks = await kopf.spawn_tasks(
        clusterwide=True, liveness_endpoint="http://0.0.0.0:8000/healthz"
    )
    tasks.append(asyncio.create_task(metrics.metrics_server()))
    await kopf.run_tasks(tasks)


asyncio.run(main())
