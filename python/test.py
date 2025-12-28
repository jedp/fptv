#!/usr/bin/env python3
"""
Run the hacky fptv/test.py to test pygame + mpv sharing GL context.

To run from within an ssh session:

sudo openvt -c 1 -s -f -w -- sh -lc \
        'env -u DISPLAY python3 -u /opt/fptv/python/test.py 2>&1 | tee /tmp/test.log'

You might have to normalize the display first:

    sudo systemctl stop fptv.service
    ps aux | grep -E 'fptv|python|mpv' | grep -v grep
    sudo systemctl restart getty@tty1.service
    sudo chvt 1
"""

if __name__ == "__main__":
    from fptv.test2 import main
    import traceback

    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
