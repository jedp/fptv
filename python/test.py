#!/usr/bin/env python3
"""
Run the hacky fptv/test.py to test pygame + mpv sharing GL context.

You might have to normalize the display first:

    sudo systemctl stop fptv.service
    ps aux | grep -E 'fptv|python|mpv' | grep -v grep
    sudo systemctl restart getty@tty1.service
    sudo chvt 1
"""

if __name__ == "__main__":
    import fptv.test
    import traceback

    try:
        raise SystemExit(fptv.test.main())
    except Exception:
        traceback.print_exc()
        raise
