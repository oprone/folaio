"""Start Folaio and open it in the browser."""
import socket
import threading
import webbrowser

import uvicorn


def free_port(preferred: int = 8765) -> int:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"Folaio is running at {url}  (press Ctrl+C to stop)")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("folaio.server:app", host="127.0.0.1", port=port, log_level="warning")
