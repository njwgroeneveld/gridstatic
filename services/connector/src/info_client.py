import os
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() != "false"
if not _SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_INFO_URL = os.getenv("HYPERLIQUID_INFO_URL", "https://api.hyperliquid.xyz/info")


class InfoClient:
    def __init__(self, connect_timeout: float = 5, read_timeout: float = 15):
        self.url = _INFO_URL
        self.timeout = (connect_timeout, read_timeout)
        self.session = requests.Session()
        self._new_session()

    def _new_session(self) -> None:
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _post(self, payload: dict):
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout, verify=_SSL_VERIFY)
        except requests.exceptions.ConnectionError:
            self.session = requests.Session()
            self._new_session()
            resp = self.session.post(self.url, json=payload, timeout=self.timeout, verify=_SSL_VERIFY)
        resp.raise_for_status()
        return resp.json()

    def get_all_mids(self) -> dict[str, float]:
        data = self._post({"type": "allMids"})
        return {k: float(v) for k, v in data.items() if v is not None}

    def get_sz_decimals(self, coin: str) -> int:
        if not hasattr(self, "_sz_cache"):
            self._sz_cache: dict[str, int] = {}
        if coin not in self._sz_cache:
            meta = self._post({"type": "meta"})
            for asset in meta.get("universe", []):
                if asset["name"] == coin:
                    self._sz_cache[coin] = asset["szDecimals"]
                    break
            else:
                self._sz_cache[coin] = 3
        return self._sz_cache[coin]

