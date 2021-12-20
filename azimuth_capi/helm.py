import asyncio
import dataclasses
import json
import logging
import shlex

import yaml


class Error(Exception):
    """
    Raised when an error occurs with a Helm command.
    """
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(stderr)


@dataclasses.dataclass
class Chart:
    """
    Class representing a Helm chart.
    """
    #: The name of the chart
    name: str
    #: The repository that the chart lives in
    repository: str
    #: The version of the chart to use
    version: str


class Release:
    """
    Class representing a Helm release.
    """
    def __init__(self, name, namespace):
        self.name = name
        self.namespace = namespace
        self.logger = logging.getLogger(__name__)

    async def _run(self, command, input = None):
        self.logger.info(
            "Executing Helm command '%s' for release '%s/%s'",
            command[1],
            self.namespace,
            self.name
        )
        # Only make stdin a pipe if we need to
        stdin = asyncio.subprocess.PIPE if input is not None else None
        proc = await asyncio.create_subprocess_shell(
            shlex.join(command),
            stdin = stdin,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE
        )
        if isinstance(input, str):
            input = input.encode()
        stdout, stderr = await proc.communicate(input)
        if proc.returncode == 0:
            return stdout
        else:
            raise Error(proc.returncode, stdout.decode(), stderr.decode())

    async def exists(self):
        """
        Tests if this release exists yet.
        """
        try:
            await self._run(["helm", "status", self.name, "--namespace", self.namespace])
        except Error:
            return False
        else:
            return True

    async def status(self):
        """
        Returns the status of the current release.
        """
        status = json.loads(
            await self._run([
                "helm",
                "status",
                self.name,
                "--namespace",
                self.namespace,
                "--output",
                "json",
            ])
        )
        # The status command does not return the chart information
        # We have to use the history command to do that
        history = json.loads(
            await self._run([
                "helm",
                "history",
                self.name,
                "--namespace",
                self.namespace,
                "--output",
                "json",
            ])
        )
        # Get the history entry corresponding to the version
        entry = next(e for e in history if e["revision"] == status["version"])
        # Stick the chart into the status and return it
        status["chart"] = entry["chart"]
        return status

    async def template_resources(self, chart, values):
        """
        Runs Helm template and returns an iterable of the resources for the release.
        """
        return yaml.safe_load_all(
            await self._run(
                [
                    "helm",
                    "template",
                    self.name,
                    chart.name,
                    "--namespace",
                    self.namespace,
                    "--repo",
                    chart.repository,
                    "--version",
                    chart.version,
                    "--values",
                    "-",
                ],
                json.dumps(values)
            )
        )
    
    async def install_or_upgrade(self, chart, values):
        """
        Install or upgrade this release using the given chart and values.
        """
        return json.loads(
            await self._run(
                [
                    "helm",
                    "upgrade",
                    self.name,
                    chart.name,
                    "--namespace",
                    self.namespace,
                    "--repo",
                    chart.repository,
                    "--version",
                    chart.version,
                    "--install",
                    "--output",
                    "json",
                    "--values",
                    "-",
                ],
                json.dumps(values)
            )
        )

    async def delete(self):
        """
        Delete this release.
        """
        await self._run(["helm", "delete", self.name, "--namespace", self.namespace])
