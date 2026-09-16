# netflix_session.py
import re
import json
import urllib.parse
import random
import time
import uuid
from typing import Optional, Dict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NetflixSessionEngine:
    """Validates Netflix session cookies, extracts account metadata, and generates nftoken login links."""

    BASE_URL = "https://www.netflix.com"

    def __init__(self):
        self.session = requests.Session()
        self._rotate_user_agent()

    def _rotate_user_agent(self):
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        ]
        self.session.headers.update({
            "User-Agent": random.choice(agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    def _random_delay(self):
        time.sleep(random.uniform(0.5, 1.5))

    # ─── Cookie Parsing ───────────────────────────

    def parse_cookies(self, raw: str) -> Dict[str, str]:
        """Parse cookies from Netscape, JSON, or header-string format."""
        raw = raw.strip()
        cookies = {}

        # ── Try JSON format first (Cookie-Editor extension) ──
        if raw.startswith("[") or raw.startswith("{"):
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            cookies[item["name"]] = item["value"]
                    return cookies
                if isinstance(data, dict) and "name" in data and "value" in data:
                    cookies[data["name"]] = data["value"]
                    return cookies
            except json.JSONDecodeError:
                pass

        # ── Netscape / header format ──
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Cookie-Editor prefixes #HttpOnly_ — strip it
            if line.startswith("#HttpOnly_"):
                line = line.replace("#HttpOnly_", "", 1)
            elif line.startswith("#"):
                continue  # Skip regular comments

            # Tab-separated Netscape (7 columns)
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()
                continue

            # Space-separated fallback
            parts = line.split()
            if len(parts) >= 7 and parts[1] in ("TRUE", "FALSE") and parts[3] in ("TRUE", "FALSE"):
                cookies[parts[5].strip()] = parts[6].strip()
                continue

            # Semicolon-separated header-style
            for kv in line.split(";"):
                kv = kv.strip()
                if "=" in kv:
                    name, value = kv.split("=", 1)
                    cookies[name.strip()] = value.strip()

        return cookies

    def extract_auth_cookies(self, raw: str) -> Optional[Dict[str, str]]:
        """Extract NetflixId and SecureNetflixId from any supported format."""
        parsed = self.parse_cookies(raw)
        nf_id = parsed.get("NetflixId")
        sec_id = parsed.get("SecureNetflixId")

        if not nf_id or not sec_id:
            return None

        return {"NetflixId": nf_id, "SecureNetflixId": sec_id}

    def set_cookies(self, cookies: Dict[str, str]):
        """Inject cookies into the requests session."""
        for name, value in cookies.items():
            self.session.cookies.set(
                name, value,
                domain=".netflix.com",
                path="/",
                secure=(name == "SecureNetflixId"),
            )

    # ─── Session Validation ──────────────────────

    def check_session(self, cookie_data: str) -> dict:
        """Main: validate cookies and return account info."""
        result = {"status": "invalid", "message": "", "account": {}}

        auth_cookies = self.extract_auth_cookies(cookie_data)
        if not auth_cookies:
            result["message"] = "Missing NetflixId or SecureNetflixId cookies"
            return result
        result["account"]["has_netflix_id"] = True
        result["account"]["has_secure_id"] = True

        self.set_cookies(auth_cookies)
        self._random_delay()

        try:
            resp = self.session.get(
                f"{self.BASE_URL}/browse",
                timeout=15,
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307):
                loc = resp.headers.get("Location", "")
                if "login" in loc.lower():
                    result["status"] = "expired"
                    result["message"] = "Session expired — redirected to login"
                    return result
            if resp.status_code != 200:
                result["status"] = "error"
                result["message"] = f"HTTP {resp.status_code}"
                return result
            if "login" in resp.url.lower():
                result["status"] = "expired"
                result["message"] = "Session expired"
                return result
        except requests.RequestException as e:
            result["status"] = "error"
            result["message"] = f"Connection error: {str(e)}"
            return result

        # Extract profile/membership from HTML
        patterns = {
            "profile_name": [r'profileName["\':\s]+([^"\',}\s]+)'],
            "plan": [
                r'currentPlan["\':\s]+["\']([^"\']+)',
                r'membershipPlan["\':\s]+["\']([^"\']+)',
                r'"membershipPlan"\s*:\s*"([^"]+)"',
            ],
            "country": [
                r'countryOfSignup["\':\s]+["\']([^"\']+)',
                r'"countryOfSignup"\s*:\s*"([^"]+)"',
            ],
            "email": [
                r'email["\':\s]+["\']([^"\']+@[^"\']+)',
                r'"primaryEmail"\s*:\s*"([^"]+)"',
            ],
        }
        for field, pats in patterns.items():
            for p in pats:
                m = re.search(p, resp.text)
                if m:
                    result["account"][field] = m.group(1)
                    break

        shakti_data = self._query_shakti()
        if shakti_data:
            result["account"]["shakti_data"] = shakti_data

        result["status"] = "valid"
        result["message"] = "Session is active"
        return result

    def _query_shakti(self) -> Optional[dict]:
        try:
            build_id = self._fetch_build_id()
            if not build_id:
                return None
            auth_url = self._fetch_auth_url() or ""

            url = f"https://www.netflix.com/api/shakti/{build_id}/pathEvaluator"
            payload = {
                "authURL": auth_url,
                "paths": ["['membershipInfo']", "['accountInfo']", "['billingInfo']"],
            }
            headers = {"Content-Type": "application/json"}
            resp = self.session.post(url, json=payload, headers=headers, timeout=20)
            return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None

    def _fetch_build_id(self) -> Optional[str]:
        try:
            resp = self.session.get(f"{self.BASE_URL}/browse", timeout=15)
            for p in [r'BUILD_IDENTIFIER["\':\s]+([a-f0-9]+)', r'/api/shakti/([a-f0-9]+)/']:
                m = re.search(p, resp.text)
                if m:
                    return m.group(1)
            return None
        except Exception:
            return None

    def _fetch_auth_url(self) -> Optional[str]:
        try:
            resp = self.session.get(f"{self.BASE_URL}/browse", timeout=15)
            m = re.search(r'authURL["\':\s]+["\']([^"\']+)', resp.text)
            return m.group(1) if m else None
        except Exception:
            return None

    # ─── Login Link Generation (Bookmarklet) ──────

    def generate_bookmarklet(self, auth_cookies: Dict[str, str]) -> str:
        """Generate a JS bookmarklet that injects cookies then redirects."""
        nf = urllib.parse.quote(auth_cookies["NetflixId"], safe="")
        sf = urllib.parse.quote(auth_cookies["SecureNetflixId"], safe="")
        js = (
            "(function(){"
            f"document.cookie='NetflixId={nf};path=/;domain=.netflix.com;max-age=2592000;';"
            f"document.cookie='SecureNetflixId={sf};path=/;domain=.netflix.com;max-age=2592000;secure;';"
            "window.location.href='https://www.netflix.com/browse';"
            "})();"
        )
        return f"javascript:{js}"

    # ─── NFToken Generation ───────────────────────

    def generate_nftoken(self, cookie_data: str) -> dict:
        """Generate an nftoken login URL from valid session cookies.

        Returns dict: {"success": True, "url": ..., "token": ..., "expires": ...}
        or {"success": False, "error": ...}
        """
        auth_cookies = self.extract_auth_cookies(cookie_data)
        if not auth_cookies:
            return {"success": False, "error": "Missing NetflixId or SecureNetflixId"}

        # ── Decode cookie values (FTL API requires decoded values) ──
        def decode(val: str) -> str:
            if "%" in val:
                try:
                    return urllib.parse.unquote(val)
                except Exception:
                    return val
            return val

        netflix_id = decode(auth_cookies["NetflixId"])
        secure_id = decode(auth_cookies["SecureNetflixId"])

        # Fixed proven ESN (from working iOS client) — random ESNs can be rejected
        esn = "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200"
        device_uuid = "90AFE39F-ADF1-4D8A-B33E-528730990FE3"

        ios_url = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"

        params = {
            "appVersion": "15.48.1",
            "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
            "device_type": "NFAPPL-02-",
            "esn": esn,
            "idiom": "phone",
            "iosVersion": "15.8.5",
            "isTablet": "false",
            "languages": "en-US",
            "locale": "en-US",
            "maxDeviceWidth": "375",
            "model": "iPhone8,1",
            "osVersion": "15.8.5",
            "preferHigherDef": "true",
            "pt": "ios",
            "screenScale": "3.0",
            "showAllDubLang": "false",
            "showAllSubLang": "false",
            "usePiano": "false",
        }

        headers = {
            "User-Agent": "Netflix/15.48.1 (iOS 15.8.5; iPhone8,1; phone)",
            "Accept": "*/*",
            "Accept-Language": "en-US;q=1",
            "Cookie": f"NetflixId={netflix_id}; SecureNetflixId={secure_id}",
            "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
            "x-netflix.context.app-version": "15.48.1",
            "x-netflix.argo.translated": "true",
            "x-netflix.context.form-factor": "phone",
            "x-netflix.context.sdk-version": "2012.4",
            "x-netflix.client.appversion": "15.48.1",
            "x-netflix.context.max-device-width": "375",
            "x-netflix.context.ab-tests": "",
            "x-netflix.tracing.cl.useractionid": str(uuid.uuid4()).upper(),
            "x-netflix.client.type": "argo",
            "x-netflix.client.ftl.esn": esn,
            "x-netflix.context.locales": "en-US",
            "x-netflix.context.top-level-uuid": device_uuid,
            "x-netflix.client.iosversion": "15.8.5",
            "accept-language": "en-US;q=1",
            "x-netflix.argo.abtests": "",
            "x-netflix.context.os-version": "15.8.5",
            "x-netflix.request.client.context": '{"appState":"foreground"}',
            "x-netflix.context.ui-flavor": "argo",
            "x-netflix.argo.nfnsm": "9",
            "x-netflix.context.pixel-density": "2.0",
            "x-netflix.request.toplevel.uuid": device_uuid,
            "x-netflix.request.client.timezoneid": "UTC",
        }

        try:
            self._random_delay()

            response = requests.get(
                ios_url,
                params=params,
                headers=headers,
                timeout=30,
                verify=False,
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }

            data = response.json()

            # Search multiple possible token locations
            token_data = (
                (((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default")
                or ((data.get("value") or {}).get("token") or {}).get("default")
                or {}
            )
            token = token_data.get("token")
            expires = token_data.get("expires")

            if not token:
                return {
                    "success": False,
                    "error": f"No token in response. Keys: {list(data.keys())[:10]} | snippet: {json.dumps(data)[:300]}",
                }

            if isinstance(expires, int) and len(str(expires)) == 13:
                expires //= 1000

            return {
                "success": True,
                "url": f"https://www.netflix.com/unsupported?nftoken={token}",
                "token": token,
                "expires": expires,
            }

        except Exception as e:
            return {"success": False, "error": f"Exception: {str(e)}"}
