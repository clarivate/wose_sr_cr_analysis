from __future__ import annotations
import os
import time
import random
from typing import Any, List, Dict, Optional
import requests

BASE_URL = 'https://wos-api.clarivate.com/api/wos'
CONNECT_TIMEOUT = 10 # seconds 
READ_TIMEOUT = 60 # seconds    
PAGE_THROTTLE = 0.25 # seconds 

class InvalidWoSQueryError(Exception):
    """Raised when the Web of Science API returns HTTP 400 for an invalid query."""
    pass

class WoSAuthenticationError(Exception):
    """Raised when the Web of Science API returns 401/403 (invalid key or insufficient access)."""
    pass

VERBOSE_REQUESTS = os.getenv('WOSESR_VERBOSE_REQUESTS', '0') == '1'

def set_verbose_requests(flag: bool) -> None:
    """Enable/disable printing of raw request URL/params for debugging."""
    global VERBOSE_REQUESTS
    VERBOSE_REQUESTS = bool(flag)

def _echo_request(params: Dict[str, Any]) -> None:
    if VERBOSE_REQUESTS:
        print(BASE_URL, params)

def _request_with_retries(url: str, headers: Dict[str, str], params: Dict[str, Any],
                          max_tries: int = 6) -> requests.Response:
    backoff = 0.5
    for attempt in range(1, max_tries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            # Handle explicit 429/5xx with backoff
            if r.status_code in (429, 500, 502, 503, 504):
                # Try to use Retry-After if provided
                ra = r.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else backoff + random.random() * 0.5
                time.sleep(wait)
                backoff = min(backoff * 2, 8.0)
                continue
            # Friendly message for 400 Bad Request
            if r.status_code == 400:
                supported_tags = (
                    "AB, AD, AI, AK, ALL, AU, CF, CI, CU, DO, DOP, EAY, ED, FD, FG, FO, "
                    "FPY, FT, GP, IS, KP, LD, OG, OO, PMID, PS, PUBL, PY, SA, SDG, SG, "
                    "SO, SU, TI, TMAC, TMIC, TMSO, TS, UT, WC, ZP"
                )

                query = params.get("usrQuery", "[unknown query]")

                raise InvalidWoSQueryError(
                    f"\nInvalid Web of Science search query.\n"
                    f"The API returned a 400 Bad Request. This is usually caused by using an unsupported field tag.\n\n"
                    f"Supported searchable field tags are:\n\n"
                    f"{supported_tags}\n"
                )
            # Friendly message for invalid/unauthorized API key
            if r.status_code in (401, 403):
                # 401 Unauthorized: invalid/expired key
                # 403 Forbidden: valid key but insufficient access (endpoint/collection/policy)
                query = params.get("usrQuery", "[unknown query]")

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

def get_response(apikey: str, params: Dict[str, Any], firstRecord: int = 1, count: int = 50) -> Dict[str, Any]:
    """Send the request to the WOS API (first page)."""
    headers = {"Accept": "application/json", "X-ApiKey": apikey}
    params = dict(params)  # do not mutate caller dict
    params['firstRecord'] = firstRecord
    params['count'] = count

    _echo_request(params)  # gated noisy echo

    r = _request_with_retries(BASE_URL, headers, params)
    time.sleep(PAGE_THROTTLE)
    return r.json()

def get_addl_results(apikey: str, params: Dict[str, Any], RecordsFound: int,
                     firstRecord: int = 1, count: int = 50, data: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Retrieve remaining pages using FULL QUERY PARAMS on each page (no qid)."""
    headers = {"Accept": "application/json", "X-ApiKey": apikey}
    if data is None:
        data = []

    base_params = dict(params)  # always reuse the original query params

    while firstRecord <= RecordsFound:
        page_params = dict(base_params)
        page_params['firstRecord'] = firstRecord
        page_params['count'] = count

        _echo_request(page_params)  # gated noisy echo

        r = _request_with_retries(BASE_URL, headers, page_params)
        js = r.json()

        # Extract RECs
        try:
            recs = js["Data"]["Records"]["records"]["REC"]
            if isinstance(recs, dict):
                recs = [recs]
        except Exception:
            recs = []

        if recs:
            data.extend(recs)
            # Progress
            print('Retrieving {} of {}'.format(len(data), RecordsFound))
        else:
            print("No 'REC' in response for firstRecord =", firstRecord)

        firstRecord += count
        time.sleep(PAGE_THROTTLE)  

    return data

def get_all_records(apikey: str, params: Dict[str, Any], firstRecord: int = 1, count: int = 50) -> List[Dict[str, Any]]:
    """Retrieve all results for a query (Short Records strongly recommended: optionView=SR)."""
    r = get_response(apikey, params, firstRecord, count)
    qr = r.get('QueryResult', {})
    total = qr.get('RecordsFound', 0)

    if total == 0:
        print("No results were retrieved for this query.")
        return []

    if total > 100000:
        print(f"Error: Query returned {total} results, max allowed is 100000.")
        return []

    data = r['Data']['Records']['records']['REC']
    if isinstance(data, dict):
        data = [data]

    if total > count:
        data = get_addl_results(apikey, params, total, firstRecord + count, count, data)

    return data
