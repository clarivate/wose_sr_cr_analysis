from __future__ import annotations
import time
import random
from typing import Any, List, Dict, Optional
import requests

class WoSAuthenticationError(Exception):
    """Raised when the WoS API returns 401/403 (invalid key or insufficient access)."""
    pass

BASE_URL = 'https://wos-api.clarivate.com/api/wos/references'
CONNECT_TIMEOUT = 10  # seconds
READ_TIMEOUT = 60     # seconds
PAGE_THROTTLE = 0.25  # seconds

def _request_with_retries(url: str, headers: Dict[str, str], params: Dict[str, Any],
                          max_tries: int = 6) -> requests.Response:
    backoff = 0.5
    for attempt in range(1, max_tries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else backoff + random.random() * 0.5
                time.sleep(wait)
                backoff = min(backoff * 2, 8.0)
                continue
            # Friendly message for invalid/unauthorized API key
            if r.status_code in (401, 403):
                raise WoSAuthenticationError(
                    f"\nWeb of Science API authentication/authorization failed (HTTP {r.status_code}).\n\n"
                    f"This usually means your API key is invalid/expired, or the key does not have access to this endpoint/collection.\n\n"
                    f"Check that EXPANDED_APIKEY (or the key you passed) is correct, active, and has WoS Expanded API access.\n"
                )            
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException:
            if attempt == max_tries:
                raise
            time.sleep(backoff + random.random() * 0.5)
            backoff = min(backoff * 2, 8.0)
    raise RuntimeError("Unreachable")

def get_response(apikey: str, params: Dict[str, Any], firstRecord: int = 1, count: int = 50, url: Optional[str] = None) -> Dict[str, Any] | None:
    headers = {"Accept": "application/json", "X-ApiKey": apikey}
    params = dict(params)
    params['count'] = count
    params['firstRecord'] = firstRecord
    req_url = BASE_URL + url if url else BASE_URL
    r = _request_with_retries(req_url, headers, params)
    time.sleep(PAGE_THROTTLE)
    return r.json()

def get_addl_results(apikey: str, params: Dict[str, Any], recordsFound: int,
                     firstRecord: int = 1, count: int = 50, data: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    headers = {"Accept": "application/json", "X-ApiKey": apikey}
    if data is None:
        data = []
    params = dict(params)
    params['count'] = count

    while firstRecord <= recordsFound:
        params['firstRecord'] = firstRecord
        r = _request_with_retries(BASE_URL, headers, params)
        js = r.json()
        try:
            page_data = js["Data"]
        except Exception:
            page_data = []
        if isinstance(page_data, list):
            data += page_data
        elif page_data:
            data.append(page_data)
        firstRecord += count
        time.sleep(PAGE_THROTTLE)

    return data

def get_all_records(apikey: str, params: Dict[str, Any], firstRecord: int = 1, count: int = 50) -> List[Dict[str, Any]]:
    r = get_response(apikey, params, firstRecord, count)
    if r is None:
        return []
    qr = r.get('QueryResult', {})
    data = r.get('Data', [])
    recordsFound = qr.get('RecordsFound', 0)    
    if recordsFound > count:
        data = get_addl_results(apikey, params, recordsFound, (firstRecord + count), count, data)
    return data
