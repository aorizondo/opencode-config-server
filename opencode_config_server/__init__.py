"""Enterprise configuration server for the OpenCode v2 harness.

Serves the `/.well-known/opencode` manifest, an authenticated corporate
configuration document, remote skills and a bootstrap installer. The harness
always ranks this configuration *below* the employee's own files, so nothing
local is ever overwritten.
"""

__version__ = "2.0.0"
