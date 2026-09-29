import os

from .http import Server


def main():
    server = Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
