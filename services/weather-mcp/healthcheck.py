import os
import sys
import urllib.error
import urllib.request

url = f"http://localhost:{os.environ.get('PORT', '8100')}/mcp"
try:
    urllib.request.urlopen(url, timeout=5)
except urllib.error.HTTPError as exc:
    # 4xx (e.g. 406 without proper Accept headers) still proves the server is up
    sys.exit(0 if exc.code < 500 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
sys.exit(0)
