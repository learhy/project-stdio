"""Tests for on_property expression parser and evaluator."""
import pytest
from studio.orchestrator.expression import Parser, evaluate, ParseError


class TestParser:
    def test_string_literal(self):
        ast = Parser('"hello"').parse()
        assert ast == {"type": "string_literal", "value": "hello"}

    def test_integer_literal(self):
        ast = Parser("42").parse()
        assert ast == {"type": "integer_literal", "value": 42}

    def test_negative_integer(self):
        ast = Parser("-5").parse()
        assert ast == {"type": "integer_literal", "value": -5}

    def test_float(self):
        ast = Parser("3.14").parse()
        assert ast == {"type": "number_literal", "value": 3.14}

    def test_boolean_true(self):
        ast = Parser("true").parse()
        assert ast == {"type": "boolean_literal", "value": True}

    def test_boolean_false(self):
        ast = Parser("false").parse()
        assert ast == {"type": "boolean_literal", "value": False}

    def test_null(self):
        ast = Parser("null").parse()
        assert ast == {"type": "null_literal", "value": None}

    def test_field_access(self):
        ast = Parser("exit_code").parse()
        assert ast == {"type": "field_access", "field": "exit_code"}

    def test_nested_field_access(self):
        ast = Parser("outputs.score").parse()
        assert ast == {
            "type": "field_access", "field": "score",
            "parent": {"type": "field_access", "field": "outputs"},
        }

    def test_deeply_nested_field(self):
        ast = Parser("a.b.c").parse()
        assert ast["field"] == "c"
        assert ast["parent"]["field"] == "b"
        assert ast["parent"]["parent"]["field"] == "a"

    def test_bracket_field_access(self):
        ast = Parser('outputs["key"]').parse()
        assert ast["field"] == "key"
        assert ast["parent"]["field"] == "outputs"

    def test_equality_comparison(self):
        ast = Parser('exit_code == 0').parse()
        assert ast["type"] == "comparison"
        assert ast["op"] == "=="

    def test_not_equal_comparison(self):
        ast = Parser('exit_code != 0').parse()
        assert ast["type"] == "comparison"
        assert ast["op"] == "!="

    def test_less_than(self):
        ast = Parser("x < 5").parse()
        assert ast["op"] == "<"

    def test_less_equal(self):
        ast = Parser("x <= 5").parse()
        assert ast["op"] == "<="

    def test_greater_than(self):
        ast = Parser("x > 5").parse()
        assert ast["op"] == ">"

    def test_greater_equal(self):
        ast = Parser("x >= 5").parse()
        assert ast["op"] == ">="

    def test_in_op(self):
        ast = Parser('x in ["a", "b"]').parse()
        assert ast["op"] == "in"

    def test_matches_op(self):
        ast = Parser('name matches "test-.*"').parse()
        assert ast["op"] == "matches"

    def test_not_expr(self):
        ast = Parser("!true").parse()
        assert ast == {"type": "not", "operands": [{"type": "boolean_literal", "value": True}]}

    def test_and_expr(self):
        ast = Parser("a && b").parse()
        assert ast["type"] == "and"

    def test_or_expr(self):
        ast = Parser("a || b").parse()
        assert ast["type"] == "or"

    def test_parenthesized(self):
        ast = Parser("(a || b) && c").parse()
        assert ast["type"] == "and"

    def test_complex_expression(self):
        ast = Parser('exit_code == 0 && outputs.score > 0.8').parse()
        assert ast["type"] == "and"

    def test_trailing_input_error(self):
        with pytest.raises(ParseError, match="Unexpected trailing"):
            Parser("x 1 2").parse()

    def test_unexpected_char_error(self):
        with pytest.raises(ParseError, match="Unexpected character"):
            Parser("~").parse()

    def test_list_literal(self):
        ast = Parser('[1, "two", true]').parse()
        assert ast["type"] == "list_literal"

    def test_precedence_not_over_compare(self):
        # Grammar: not_expr = ["!"] compare, so ! binds to the whole comparison
        # !x == 5 means !(x == 5)
        ast = Parser("!x == 5").parse()
        assert ast["type"] == "not"
        assert ast["operands"][0]["type"] == "comparison"


class TestEvaluator:
    def test_string_equality(self):
        assert evaluate('"hello" == "hello"', {}) is True

    def test_string_inequality(self):
        assert evaluate('"hello" != "world"', {}) is True

    def test_integer_equality(self):
        assert evaluate("exit_code == 0", {"exit_code": 0}) is True

    def test_integer_inequality(self):
        assert evaluate("exit_code == 0", {"exit_code": 1}) is False

    def test_field_access_from_context(self):
        assert evaluate("exit_code == 0", {"exit_code": 0}) is True

    def test_nested_field_access(self):
        ctx = {"outputs": {"score": 0.95}}
        assert evaluate("outputs.score > 0.9", ctx) is True

    def test_not(self):
        assert evaluate("!true", {}) is False

    def test_and_both_true(self):
        assert evaluate("true && true", {}) is True

    def test_and_one_false(self):
        assert evaluate("true && false", {}) is False

    def test_or_one_true(self):
        assert evaluate("true || false", {}) is True

    def test_or_both_false(self):
        assert evaluate("false || false", {}) is False

    def test_less_than(self):
        ctx = {"count": 3}
        assert evaluate("count < 5", ctx) is True
        assert evaluate("count < 2", ctx) is False

    def test_greater_than(self):
        ctx = {"count": 5}
        assert evaluate("count > 2", ctx) is True

    def test_in_op_list(self):
        assert evaluate('"a" in ["a", "b", "c"]', {}) is True

    def test_in_op_string(self):
        assert evaluate('"test" in "testing"', {}) is True

    def test_in_op_false(self):
        assert evaluate('"z" in ["a", "b"]', {}) is False

    def test_matches_regex(self):
        assert evaluate('name matches "^test-\\d+$"', {"name": "test-42"}) is True

    def test_matches_regex_no_match(self):
        assert evaluate('name matches "^prod-\\d+$"', {"name": "test-42"}) is False

    def test_complex_and(self):
        ctx = {"exit_code": 0, "outputs": {"score": 0.9}}
        assert evaluate("exit_code == 0 && outputs.score > 0.8", ctx) is True

    def test_complex_or(self):
        ctx = {"status": "error"}
        assert evaluate('status == "ok" || status == "error"', ctx) is True

    def test_bool_field(self):
        ctx = {"ready": True}
        assert evaluate("ready == true", ctx) is True

    def test_null_check(self):
        ctx = {"value": None}
        assert evaluate("value == null", ctx) is True

    def test_non_comparable_types(self):
        assert evaluate('"x" > 5', {}) is False

    def test_parse_error_fail_closed(self):
        assert evaluate("~~~", {}) is False

    def test_invalid_field_in_comparison(self):
        assert evaluate("nonexistent > 5", {}) is False

    def test_complex_precedence(self):
        ctx = {"a": True, "b": False, "c": True}
        assert evaluate("a && (b || c)", ctx) is True
        assert evaluate("a && b || c", ctx) is True  # && binds tighter

    def test_empty_context(self):
        assert evaluate("x == null", {}) is True

    def test_number_comparison_different_types_float_int(self):
        assert evaluate("3.0 == 3", {}) is True
