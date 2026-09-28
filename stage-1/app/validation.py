"""Explicit JSON boundary validation without coercing strings or booleans."""
import math
import re


class APIError(Exception):
    def __init__(self, status=422, code="validation_failed"):
        self.status = status
        self.code = code
        super().__init__(code)


def require(condition, status=422, code="validation_failed"):
    if not condition:
        raise APIError(status, code)


def field(obj, name, kind):
    require(name in obj)
    value = obj[name]
    require(type(value) is kind, 400, "malformed_request")
    return value


def identifier(obj, name):
    value = field(obj, name, str)
    require(0 < len(value) <= 64)
    return value


def integer(obj, name, minimum=1):
    value = field(obj, name, int)
    require(value >= minimum)
    return value


def party(obj):
    value = obj.get("party_size")
    require(type(value) is int and value >= 1)
    return value


def email_password(body):
    email = field(body, "email", str)
    password = field(body, "password", str)
    require(re.fullmatch(r"[^\s@]+@[^\s@]+", email) is not None)
    return email, password


def json_equal(a, b):
    # JSON numbers share one type, but true is not the number 1.
    if type(a) in (int, float) and type(b) in (int, float):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return a == b


def finite_json(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(finite_json(v) for v in value)
    if isinstance(value, dict):
        return all(finite_json(v) for v in value.values())
    return True
