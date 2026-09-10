from boss_secretary.ingress.slack import *  # noqa: F401,F403
from boss_secretary.ingress.slack import main

if __name__ == "__main__":
    import sys
    raise SystemExit(main())
