"""The terminal backend the fleet runs its sessions in.

``tmux`` on Linux, macOS and WSL; ``conpty`` (Windows' own pseudo-console) on
Windows, where there is no tmux. Both modules expose the same functions, so
callers write ``term.capture(...)`` and never ask which one they have.
``config.backend`` overrides the choice.
"""

from maestro import config

if config.BACKEND == "conpty":
    from maestro import conpty as backend
else:
    from maestro import tmux as backend

NAME = config.BACKEND
