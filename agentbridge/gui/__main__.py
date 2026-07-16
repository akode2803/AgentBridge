"""``python -m agentbridge.gui --root <mesh-root>``

V126: the import here must be the CHEAP fastboot module — it binds the
port and opens the app window in ~100 ms, then loads the heavy server.
"""

import sys

from .fastboot import main

sys.exit(main())
