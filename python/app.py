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
    import faulthandler, signal
    import fptv.kiosk

    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1)

    app = fptv.kiosk.FPTV()
    exit_code = 0
    try:
        app.mainloop()
    except KeyboardInterrupt:
        exit_code = app.shutdown()
        if exit_code == 0:
            print("App shutdown successful")
        else:
            print(f"App shutdown returned code {exit_code}")

    raise (SystemExit(exit_code))

