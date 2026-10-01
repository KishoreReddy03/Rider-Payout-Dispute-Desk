"""Public API and PaySwift client wrapper for black-box evaluator.

IMPORTANT: This client MUST ONLY interact with public HTTP endpoints.
It must never access internal database tables or private Python code.
"""

import json
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional


class PublicAPIClient:
    """Client for interacting with the public HTTP interface of Rider Dispute Desk."""

    def __init__(self, base_url: str, payswift_url: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.payswift_url = payswift_url.rstrip("/") if payswift_url else None

        # Auto-discover PaySwift URL if not explicitly provided
        if not self.payswift_url:
            try:
                health = self.get_health()
                if "payswift_url" in health and health["payswift_url"]:
                    self.payswift_url = health["payswift_url"].rstrip("/")
            except Exception:
                self.payswift_url = "http://localhost:8081"

    def _request(self, method: str, url: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req_data = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                err_json = json.loads(body)
            except Exception:
                err_json = {"detail": body}
            return {"_status_code": exc.code, "_error": err_json}

    def get_health(self) -> Dict[str, Any]:
        """GET /health"""
        return self._request("GET", f"{self.base_url}/health")

    def post_message(self, message_id: str, rider_id: str, text: str, received_at: Optional[str] = None) -> Dict[str, Any]:
        """POST /messages"""
        payload = {
            "message_id": message_id,
            "rider_id": rider_id,
            "text": text,
        }
        if received_at:
            payload["received_at"] = received_at
        return self._request("POST", f"{self.base_url}/messages", payload)

    def get_trace(self, rider_id: str) -> List[Dict[str, Any]]:
        """GET /trace/{rider_id}"""
        res = self._request("GET", f"{self.base_url}/trace/{rider_id}")
        if isinstance(res, list):
            return res
        return []

    def get_ops_pending(self) -> List[Dict[str, Any]]:
        """GET /ops/pending"""
        res = self._request("GET", f"{self.base_url}/ops/pending")
        if isinstance(res, list):
            return res
        return []

    def approve_ops_decision(self, decision_id: int) -> Dict[str, Any]:
        """POST /ops/decisions/{decision_id}/approve"""
        return self._request("POST", f"{self.base_url}/ops/decisions/{decision_id}/approve")

    def reject_ops_decision(self, decision_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
        """POST /ops/decisions/{decision_id}/reject"""
        return self._request("POST", f"{self.base_url}/ops/decisions/{decision_id}/reject", {"reason": reason})

    def escalate_ops_decision(self, decision_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
        """POST /ops/decisions/{decision_id}/escalate"""
        return self._request("POST", f"{self.base_url}/ops/decisions/{decision_id}/escalate", {"reason": reason})

    def get_payswift_payouts(self, rider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET PaySwift /v1/payouts?rider_id={rider_id}"""
        if not self.payswift_url:
            return []
        url = f"{self.payswift_url}/v1/payouts"
        if rider_id:
            url += f"?rider_id={rider_id}"
        res = self._request("GET", url)
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "payouts" in res:
            return res["payouts"]
        return []
