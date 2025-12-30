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


    def signal_handler(signum, frame):
        exit_code = app.shutdown()
        if exit_code == 0:
            print("App shutdown successful")
        else:
            print(f"App shutdown returned code {exit_code}")
        raise SystemExit(exit_code)


    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        app.mainloop()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Unexpected error: {e}")
        try:
            app.shutdown()
        except Exception as shutdown_err:
            print(f"Error during shutdown: {shutdown_err}")
        raise
