"""Error types shared by the service, the HTTP server and the CLI."""


class TeamSkillsError(Exception):
    """Base class. ``code`` is the stable machine-readable name used on the wire."""

    code = "error"
    http_status = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(TeamSkillsError):
    code = "not_found"
    http_status = 404


class Forbidden(TeamSkillsError):
    code = "forbidden"
    http_status = 403


class Invalid(TeamSkillsError):
    code = "invalid"
    http_status = 400


class Conflict(TeamSkillsError):
    code = "conflict"
    http_status = 409


class Unauthorized(TeamSkillsError):
    code = "unauthorized"
    http_status = 401


BY_CODE = {cls.code: cls for cls in (NotFound, Forbidden, Invalid, Conflict, Unauthorized)}


def from_code(code: str, message: str) -> TeamSkillsError:
    return BY_CODE.get(code, TeamSkillsError)(message)
