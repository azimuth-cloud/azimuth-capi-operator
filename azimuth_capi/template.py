import jinja2
import yaml

from .config import settings
from . import models


class Loader:
    """
    Class for returning objects created by rendering YAML templates from this package.
    """
    def __init__(self, **globals):
        # Create the package loader for the parent module of this one
        loader = jinja2.PackageLoader(self.__module__.rsplit(".", maxsplit = 1)[0])
        self.env = jinja2.Environment(loader = loader, autoescape = False)
        self.env.globals.update(globals)
        # Make a toyaml filter available
        self.env.filters["toyaml"] = yaml.safe_dump

    def load(self, template, **params):
        """
        Render the specified template with the given params, load the result as
        YAML and return it.
        """
        return yaml.safe_load(self.env.get_template(template).render(**params))


default_loader = Loader(settings = settings, models = models)
