"""A session binds an actor to a team and dispatches operations.

``LocalSession`` calls the ``Service`` in-process against a local database;
``RemoteSession`` sends the same operations to a ``skillcurrent serve``
instance over HTTP. Both expose ``call(op, **args)`` so the CLI and the
installer never care which one they hold.
"""

import json
import urllib.error
import urllib.request

from .errors import Invalid, SkillCurrentError, Unauthorized, from_code
from .service import RPC_OPS, Service


class LocalSession:
    def __init__(self, service: Service, team: str, actor: str):
        self.service = service
        self.team = team
        self.actor = actor

    def call(self, op: str, **args):
        if op not in RPC_OPS:
            raise Invalid(f"unknown operation {op!r}")
        return getattr(self.service, op)(self.team, self.actor, **args)


class RemoteSession:
    def __init__(self, url: str, token: str | None = None, team: str | None = None, actor: str | None = None, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self.team = team
        self.actor = actor
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.team:
            headers["X-SkillCurrent-Team"] = self.team
        if self.actor:
            headers["X-SkillCurrent-User"] = self.actor
        return headers

    def call(self, op: str, **args):
        body = json.dumps({"op": op, "args": args}).encode("utf-8")
        req = urllib.request.Request(self.url + "/api/rpc", data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise SkillCurrentError(f"server returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SkillCurrentError(f"cannot reach {self.url}: {exc.reason}") from exc
        if payload.get("ok"):
            return payload.get("result")
        err = payload.get("error") or {}
        raise from_code(err.get("code", "error"), err.get("message", "unknown error"))

    def whoami(self) -> dict:
        info = self.call("whoami")
        self.team = info["team"]["slug"]
        self.actor = info["member"]["handle"]
        return info


def require_identity(session) -> None:
    """Make sure a session knows its team and actor (a remote session learns them from the server)."""
    if not session.team or not session.actor:
        if isinstance(session, RemoteSession):
            session.whoami()
        else:
            raise Unauthorized("a team and an actor handle are required")
