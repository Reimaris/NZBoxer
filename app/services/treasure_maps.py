# TEMPORARY m59 alias — deleted in ticket 12
import sys
from typing import TYPE_CHECKING

from app.services.indexers import newznab as _m

if TYPE_CHECKING:
    from app.services.indexers.newznab import *  # noqa: F403

sys.modules[__name__] = _m
