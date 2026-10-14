# TEMPORARY m59 alias — deleted in ticket 14
import sys

from app.services.watchlists import simkl as _m

sys.modules[__name__] = _m
