from .core import Client, ApplicationClient
from .model import Job, Service, File, Resources

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions
