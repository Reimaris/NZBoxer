# TEMPORARY m59 alias — deleted in ticket 16
import sys

from app.services.downloaders import torbox as _m

sys.modules[__name__] = _m
