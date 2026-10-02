import os
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() != "false"
if not _SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"


def _info_url() -> str:
    """Prices come from the network the orders go to. A testnet grid priced off
    mainnet places its orders at prices that do not exist on testnet.
    HYPERLIQUID_INFO_URL still overrides, for a proxy or a mirror."""
    override = os.getenv("HYPERLIQUID_INFO_URL")
    if override:
        return override
    testnet = os.getenv("HYPERLIQUID_TESTNET", "true").lower() != "false"
    return _TESTNET_INFO_URL if testnet else _MAINNET_INFO_URL


class InfoClient:
    def __init__(self, connect_timeout: float = 5, read_timeout: float = 15):
        self.url = _info_url()
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

    def get_mark_price(self, coin: str) -> float:
        """The mark price, not the mid. On a thin book the mid is just the middle
        of a wide spread -- and a grid's own buys move it, so it would chase its
        own orders. The mark price is anchored to the oracle."""
        dex = coin.split(":", 1)[0] if ":" in coin else ""
        meta, ctxs = self._post({"type": "metaAndAssetCtxs", "dex": dex})
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset["name"] == coin:
                return float(ctx["markPx"])
        raise KeyError(f"{coin} is not listed on dex '{dex}'")

    def get_sz_decimals(self, coin: str) -> int:
        if not hasattr(self, "_sz_cache"):
            self._sz_cache: dict[str, int] = {}
        if coin not in self._sz_cache:
            dex = coin.split(":", 1)[0] if ":" in coin else ""
            meta = self._post({"type": "meta", "dex": dex})
            for asset in meta.get("universe", []):
                if asset["name"] == coin:
                    self._sz_cache[coin] = asset["szDecimals"]
                    break
            else:
                raise KeyError(f"{coin} is not listed on dex '{dex}'")
        return self._sz_cache[coin]
