import asyncio
import logging
import os

import kopf

from . import metrics

LOG = logging.getLogger(__name__)


def configure_kopf_logging() -> None:
    """
    Configure logging for the operator in the kopf framework
    """
    # Cast an env var to an official Python logging level
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    # Map that logging level to kopf configuration
    log_debug = log_level <= logging.DEBUG
    log_verbose = log_level <= logging.DEBUG
    log_quiet = log_level >= logging.WARNING
    LOG.warning(
        f"LOG {log_level_name} - debug: {log_debug} verbose {log_verbose} "
        "quiet {log_quiet}"
    )

    kopf.configure(
        log_prefix=True,
        debug=log_debug,
        verbose=log_verbose,
        quiet=log_quiet,
        log_format=kopf.LogFormat.FULL,
    )


async def main():
    """
    Run the operator and the metrics server together.
    """
    # This import is required to pick up the operator handlers
    from . import operator  # noqa: F401

    configure_kopf_logging()
    tasks = await kopf.spawn_tasks(
        clusterwide=True, liveness_endpoint="http://0.0.0.0:8000/healthz"
    )
    tasks.append(asyncio.create_task(metrics.metrics_server()))
    await kopf.run_tasks(tasks)


asyncio.run(main())
