#!/usr/bin/env python3

if __name__ == "__main__":
    import kiosk

    app = kiosk.FPTV()
    exit_code = 0
    try:
        app.mainloop()
    except KeyboardInterrupt:
        exit_code = app.shutdown()
        if exit_code == 0:
            print("App shutdown successful")
        else:
            print(r"App shutdown failed: Result code {result}")

    raise (SystemExit(exit_code))
