"""
Client for the BDR Thermea cloud API used by the BAXI Connect / Remeha Home / De Dietrich apps.

The thermostat itself has no local HTTP/TCP service (verified: full 1-65535 TCP scan and
UDP probes return nothing), so all control goes through the manufacturer cloud.

Authentication is Azure AD B2C (the same policy the official apps use), then a bearer
token against https://api.bdrthermea.net/Mobile/api.

Only the Python standard library is used, so this runs anywhere with python3 >= 3.8.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Platform constants (observed from the official apps) ---------------------

B2C_HOST = "https://remehalogin.bdrthermea.net/bdrb2cprod.onmicrosoft.com"
POLICY = "B2C_1A_RPSignUpSignInNewRoomV3.1"
CLIENT_ID = "6ce007c6-0628-419e-88f4-bee2e6418eec"
SCOPE = (
    "openid https://bdrb2cprod.onmicrosoft.com/iotdevice/user_impersonation offline_access"
)

API_BASE = "https://api.bdrthermea.net/Mobile/api"
API_KEY = "df605c5470d846fc91e848b1cc653ddf"  # Ocp-Apim-Subscription-Key baked into the apps

DEFAULT_TOKEN_FILE = Path(__file__).resolve().parent / ".baxi_token.json"


class BaxiError(RuntimeError):
    """An error talking to the Baxi/BDR cloud."""


class AuthError(BaxiError):
    """Bad credentials, expired refresh token, etc."""


# --- tiny HTTP helpers --------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfacing 3xx as HTTPError so we can read Location


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class Http:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
        self.no_redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_ssl_context()),
            _NoRedirect,
        )

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        follow: bool = True,
        timeout: float = 30.0,
    ):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; baxi-cli)")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        opener = self.opener if follow else self.no_redirect_opener
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e  # caller inspects status / Location

    def cookie(self, name: str) -> Optional[str]:
        for c in self.jar:
            if c.name.lower() == name.lower():
                return c.value
        return None


def _json(resp) -> Any:
    raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise BaxiError(f"Expected JSON from {getattr(resp, 'url', '?')}, got: {raw[:300]}")


# --- the API client -----------------------------------------------------------


class BaxiCloud:
    def __init__(
        self,
        brand: str = "baxi",
        token_file: Path | str = DEFAULT_TOKEN_FILE,
        lang: str = "es",
    ) -> None:
        self.brand = brand
        self.lang = lang
        self.token_file = Path(token_file)
        self.http = Http()
        self.token: Dict[str, Any] = {}

    # -- auth ---------------------------------------------------------------

    def _authorize_url(self, state: str, challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": f"com.b2c.{self.brand}app://login-callback",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "p": POLICY,
            "brand": self.brand,
            "lang": self.lang,
            "nonce": "defaultNonce",
            "prompt": "login",
            "signUp": "False",
        }
        return f"{B2C_HOST}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """Perform the same B2C sign-in flow the mobile app performs."""
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(16)

        resp = self.http.request(self._authorize_url(state, challenge))
        if resp.status != 200:
            raise AuthError(f"Login page returned HTTP {resp.status}")
        body = resp.read().decode("utf-8", "replace")
        request_id = resp.headers.get("x-request-id")
        if not request_id:
            raise AuthError("Login page did not return an x-request-id header")

        csrf = self.http.cookie("x-ms-cpim-csrf")
        if not csrf:
            raise AuthError("Login page did not set the x-ms-cpim-csrf cookie")

        tx = base64.urlsafe_b64encode(
            json.dumps({"TID": request_id}).encode()
        ).decode().rstrip("=")

        # 1. submit the credentials
        self_asserted = (
            f"{B2C_HOST}/{POLICY}/SelfAsserted?"
            + urllib.parse.urlencode({"tx": "StateProperties=" + tx, "p": POLICY})
        )
        resp = self.http.request(
            self_asserted,
            method="POST",
            data=urllib.parse.urlencode(
                {
                    "request_type": "RESPONSE",
                    "signInName": email,
                    "password": password,
                }
            ).encode(),
            headers={
                "x-csrf-token": csrf,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            result = json.loads(resp.read().decode("utf-8", "replace"))
        except ValueError:
            raise AuthError(f"Unexpected login response (HTTP {resp.status})")

        if result.get("status") != "200":
            msg = result.get("message") or result.get("errorCode") or "unknown error"
            if result.get("errorCode") == "AADB2C90053":
                raise AuthError("Wrong email or password (AADB2C90053)")
            raise AuthError(f"Login rejected: {msg}")

        # 2. collect the authorization code
        confirmed = (
            f"{B2C_HOST}/{POLICY}/api/CombinedSigninAndSignup/confirmed?"
            + urllib.parse.urlencode(
                {
                    "rememberMe": "false",
                    "csrf_token": csrf,
                    "tx": "StateProperties=" + tx,
                    "p": POLICY,
                }
            )
        )
        resp = self.http.request(confirmed, follow=False)
        location = resp.headers.get("Location") if hasattr(resp, "headers") else None
        if not location:
            raise AuthError(f"No redirect with authorization code (HTTP {resp.status})")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code")
        if not code:
            raise AuthError(f"Redirect had no code: {location[:200]}")

        # 3. exchange it for tokens
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code[0],
                "redirect_uri": f"com.b2c.{self.brand}app://login-callback",
                "code_verifier": verifier,
                "client_id": CLIENT_ID,
            }
        )

    def _token_request(self, params: Dict[str, str]) -> Dict[str, Any]:
        resp = self.http.request(
            f"{B2C_HOST}/oauth2/v2.0/token?" + urllib.parse.urlencode({"p": POLICY}),
            method="POST",
            data=urllib.parse.urlencode(params).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = _json(resp)
        if resp.status != 200 or "access_token" not in payload:
            err = payload.get("error_description") or payload.get("error") or payload
            raise AuthError(f"Token request failed: {err}")
        payload["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 60
        self.token = payload
        return payload

    # -- token storage / refresh -------------------------------------------

    def save_token(self) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(self.token, indent=2))
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def load_token(self) -> bool:
        try:
            self.token = json.loads(self.token_file.read_text())
            return bool(self.token.get("access_token"))
        except (OSError, ValueError):
            return False

    def ensure_token(self) -> str:
        if not self.token and not self.load_token():
            raise AuthError("Not logged in: run `python3 baxi.py login` first")
        if self.token.get("expires_at", 0) > time.time():
            return self.token["access_token"]
        if not self.token.get("refresh_token"):
            raise AuthError("Token expired and no refresh token: log in again")
        self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
                "client_id": CLIENT_ID,
            }
        )
        self.save_token()
        return self.token["access_token"]

    # -- API calls ----------------------------------------------------------

    def _api(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        token = self.ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": API_KEY,
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        resp = self.http.request(
            API_BASE + path, method=method, data=data, headers=headers
        )
        if resp.status == 401:
            raise AuthError("API rejected the token (HTTP 401) - log in again")
        if resp.status >= 400:
            raw = resp.read().decode("utf-8", "replace")[:400]
            raise BaxiError(f"{method} {path} -> HTTP {resp.status}: {raw}")
        if resp.status == 204 or method == "POST" and resp.length == 0:
            return None
        try:
            return _json(resp)
        except BaxiError:
            return None

    # Read

    def dashboard(self) -> Dict[str, Any]:
        return self._api("GET", "/homes/dashboard")

    def technical_details(self, appliance_id: str) -> Dict[str, Any]:
        return self._api("GET", f"/appliances/{appliance_id}/technicaldetails")

    # Write

    def set_manual(self, zone_id: str, temperature: float) -> None:
        """Hold this temperature indefinitely (leaves the weekly program)."""
        self._api(
            "POST",
            f"/climate-zones/{zone_id}/modes/manual",
            {"roomTemperatureSetPoint": float(temperature)},
        )

    def set_override(self, zone_id: str, temperature: float) -> None:
        """Temporarily override the program until the next scheduled change."""
        self._api(
            "POST",
            f"/climate-zones/{zone_id}/modes/temporary-override",
            {"roomTemperatureSetPoint": float(temperature)},
        )

    def set_schedule(self, zone_id: str, heating_program_id: int = 1) -> None:
        self._api(
            "POST",
            f"/climate-zones/{zone_id}/modes/schedule",
            {"heatingProgramId": int(heating_program_id)},
        )

    def set_off(self, zone_id: str) -> None:
        """Anti-frost / off."""
        self._api("POST", f"/climate-zones/{zone_id}/modes/anti-frost")

    def set_fireplace(self, zone_id: str, enabled: bool) -> None:
        self._api(
            "POST",
            f"/climate-zones/{zone_id}/modes/fireplacemode",
            {"fireplaceModeActive": bool(enabled)},
        )

    def activate_program(self, zone_id: str, program_id: int) -> None:
        self._api(
            "POST",
            f"/climate-zones/{zone_id}/time-programs/heating/{int(program_id)}/activate",
        )

    def set_hot_water(self, dhw_zone_id: str, comfort: Optional[float] = None,
                      reduced: Optional[float] = None) -> None:
        payload = {}
        if comfort is not None:
            payload["comfortSetpoint"] = float(comfort)
        if reduced is not None:
            payload["reducedSetpoint"] = float(reduced)
        if not payload:
            raise ValueError("give comfort and/or reduced")
        self._api("POST", f"/hot-water-zones/{dhw_zone_id}/setpoints", payload)

    # -- convenience --------------------------------------------------------

    def zones(self) -> List[Dict[str, Any]]:
        """Flatten the dashboard into heating zones with their appliance context."""
        out = []
        for app in self.dashboard().get("appliances", []):
            for z in app.get("climateZones", []):
                out.append({"appliance": app, "zone": z})
        return out

    def find_zone(self, index: int = 0) -> Dict[str, Any]:
        zones = self.zones()
        if not zones:
            raise BaxiError(
                "No heating zone in this account. The thermostat may not be paired to "
                "this cloud (older BAXI Connect units used a backend that is now offline)."
            )
        if index >= len(zones):
            raise BaxiError(f"Zone {index} does not exist (found {len(zones)})")
        return zones[index]
