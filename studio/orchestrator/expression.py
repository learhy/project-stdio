"""Recursive-descent parser and evaluator for the on_property expression sublanguage.

Grammar:
  expression = or_expr
  or_expr    = and_expr , { "||" , and_expr }
  and_expr   = not_expr , { "&&" , not_expr }
  not_expr   = [ "!" ] , compare
  compare    = primary , [ compare_op , primary ]
  compare_op = "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "matches"
  primary    = literal | path | "(" , expression , ")"
  path       = identifier , { path_step }
  path_step  = "." , identifier | "[" , string_literal , "]"

No I/O, no clock, no randomness. Evaluation errors fail-closed (return False).
"""
from __future__ import annotations

import re
from typing import Any


class ParseError(Exception):
    pass


class EvalError(Exception):
    pass


# ── Token kinds ───────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")


# ── Parser ─────────────────────────────────────────────────────────────────────

class Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    @property
    def _eof(self) -> bool:
        return self.pos >= len(self.source)

    def _peek(self) -> str:
        return "" if self._eof else self.source[self.pos]

    def _advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        return ch

    def _skip_ws(self) -> None:
        while self.pos < len(self.source) and self.source[self.pos] in (" ", "\t", "\n", "\r"):
            self.pos += 1

    def _expect(self, s: str) -> None:
        self._skip_ws()
        end = self.pos + len(s)
        if self.source[self.pos:end] == s:
            self.pos = end
            return
        raise ParseError(f"Expected '{s}' at position {self.pos}")

    def parse(self) -> dict[str, Any]:
        self._skip_ws()
        ast = self._parse_or()
        self._skip_ws()
        if not self._eof:
            raise ParseError(f"Unexpected trailing input at position {self.pos}: '{self.source[self.pos:]}'")
        return ast

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while True:
            self._skip_ws()
            if self.source[self.pos:self.pos + 2] == "||":
                self.pos += 2
                right = self._parse_and()
                left = {"type": "or", "operands": [left, right]}
            else:
                break
        return left

    def _parse_and(self) -> dict:
        left = self._parse_not()
        while True:
            self._skip_ws()
            if self.source[self.pos:self.pos + 2] == "&&":
                self.pos += 2
                right = self._parse_not()
                left = {"type": "and", "operands": [left, right]}
            else:
                break
        return left

    def _parse_not(self) -> dict:
        self._skip_ws()
        if self._peek() == "!":
            self._advance()
            operand = self._parse_not()
            return {"type": "not", "operands": [operand]}
        return self._parse_compare()

    def _parse_compare(self) -> dict:
        left = self._parse_primary()
        self._skip_ws()

        # Check for compare op
        pos_before = self.pos
        op = None
        for candidate in ("==", "!=", "<=", ">=", "<", ">", "in", "matches"):
            end = self.pos + len(candidate)
            if self.source[self.pos:end] == candidate:
                op = candidate
                self.pos = end
                break

        if op is None:
            return left

        # 'in' and 'matches' need a keyword-like boundary check
        if op in ("in", "matches"):
            after = self.source[self.pos:self.pos]
            # We already consumed the word; check next char is whitespace/bracket/eof
            pass

        right = self._parse_primary()
        return {"type": "comparison", "left": left, "op": op, "right": right}

    def _parse_primary(self) -> dict:
        self._skip_ws()
        ch = self._peek()

        if ch == "(":
            self._advance()
            expr = self._parse_or()
            self._expect(")")
            return expr

        if ch == '"':
            return {"type": "string_literal", "value": self._parse_string()}

        if ch == "t" and self.source[self.pos:self.pos + 4] == "true":
            self.pos += 4
            return {"type": "boolean_literal", "value": True}

        if ch == "f" and self.source[self.pos:self.pos + 5] == "false":
            self.pos += 5
            return {"type": "boolean_literal", "value": False}

        if ch == "n" and self.source[self.pos:self.pos + 4] == "null":
            self.pos += 4
            return {"type": "null_literal", "value": None}

        if ch == "[":
            return self._parse_list()

        if ch == "-" or ch.isdigit():
            return self._parse_number()

        if _IDENT_RE.match(self.source[self.pos:]):
            m = _IDENT_RE.match(self.source[self.pos:])
            assert m
            name = m.group()
            self.pos += len(name)
            node: dict[str, Any] = {"type": "field_access", "field": name}
            # Parse path steps
            while True:
                self._skip_ws()
                if self._peek() == ".":
                    self._advance()
                    m2 = _IDENT_RE.match(self.source[self.pos:])
                    if not m2:
                        raise ParseError(f"Expected identifier after '.' at position {self.pos}")
                    name = m2.group()
                    self.pos += len(name)
                    node = {"type": "field_access", "field": name, "parent": node}
                elif self._peek() == "[":
                    self._advance()
                    self._skip_ws()
                    if self._peek() == "'" or self._peek() == '"':
                        key = self._parse_string()
                        node = {"type": "field_access", "field": key, "parent": node}
                    else:
                        idx = self._parse_number()
                        node = {"type": "field_access", "field": idx["value"], "parent": node}
                    self._skip_ws()
                    self._expect("]")
                else:
                    break
            return node

        raise ParseError(f"Unexpected character '{ch}' at position {self.pos}")

    def _parse_string(self) -> str:
        quote = self._advance()
        result = []
        while not self._eof and self._peek() != quote:
            if self._peek() == "\\":
                self._advance()
                esc = self._advance()
                if esc == "n":
                    result.append("\n")
                elif esc == "t":
                    result.append("\t")
                elif esc == "\\":
                    result.append("\\")
                elif esc == quote:
                    result.append(quote)
                else:
                    result.append("\\" + esc)
            else:
                result.append(self._advance())
        if self._eof:
            raise ParseError("Unterminated string literal")
        self._advance()  # closing quote
        return "".join(result)

    def _parse_list(self) -> dict:
        self._advance()  # '['
        items = []
        self._skip_ws()
        if self._peek() != "]":
            items.append(self._parse_primary())
            while True:
                self._skip_ws()
                if self._peek() == ",":
                    self._advance()
                    items.append(self._parse_primary())
                else:
                    break
        self._expect("]")
        return {"type": "list_literal", "value": items}

    def _parse_number(self) -> dict:
        m = _NUMBER_RE.match(self.source[self.pos:])
        if not m:
            raise ParseError(f"Expected number at position {self.pos}")
        self.pos += len(m.group())
        val = m.group()
        if "." in val:
            return {"type": "number_literal", "value": float(val)}
        return {"type": "integer_literal", "value": int(val)}


# ── Evaluator ─────────────────────────────────────────────────────────────────

def evaluate(expression: str, context: dict[str, Any]) -> bool:
    """Parse and evaluate an on_property expression against a node output context.

    Context shape: {outputs: {...}, artifacts: [...], exit_code: int, report: {...}}
    Returns bool. Parse/eval errors fail-closed (return False).
    """
    try:
        parser = Parser(expression)
        ast = parser.parse()
        result = _eval(ast, context)
        if isinstance(result, bool):
            return result
        return False
    except (ParseError, EvalError, Exception):
        return False


def _eval(node: dict[str, Any], ctx: dict[str, Any]) -> Any:
    typ = node.get("type", "")

    if typ == "or":
        for op in node.get("operands", []):
            if _eval(op, ctx):
                return True
        return False

    elif typ == "and":
        for op in node.get("operands", []):
            if not _eval(op, ctx):
                return False
        return True

    elif typ == "not":
        return not _eval(node["operands"][0], ctx)

    elif typ == "comparison":
        left = _eval(node["left"], ctx)
        right = _eval(node["right"], ctx)
        return _compare(left, node["op"], right)

    elif typ == "field_access":
        parent = node.get("parent")
        if parent:
            parent_val = _eval(parent, ctx)
            if isinstance(parent_val, dict):
                return parent_val.get(node["field"])
            elif isinstance(parent_val, list):
                try:
                    idx = int(node["field"])
                    return parent_val[idx]
                except (ValueError, IndexError):
                    raise EvalError(f"List index out of bounds: {node['field']}")
            return None
        return ctx.get(node["field"])

    elif typ == "string_literal":
        return node["value"]

    elif typ == "integer_literal":
        return node["value"]

    elif typ == "number_literal":
        return node["value"]

    elif typ == "boolean_literal":
        return node["value"]

    elif typ == "null_literal":
        return None

    elif typ == "list_literal":
        return [_eval(item, ctx) for item in node.get("value", [])]

    raise EvalError(f"Unknown AST node type: {typ}")


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "==":
        return left == right
    elif op == "!=":
        return left != right
    elif op in ("<", "<=", ">", ">="):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        if op == "<":
            return left < right
        elif op == "<=":
            return left <= right
        elif op == ">":
            return left > right
        elif op == ">=":
            return left >= right
    elif op == "in":
        if isinstance(right, (list, tuple, str)):
            return left in right
        return False
    elif op == "matches":
        try:
            return bool(re.search(str(right), str(left)))
        except re.error:
            return False
    return False
