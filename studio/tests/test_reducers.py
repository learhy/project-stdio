"""Tests for the reducer registry and built-in reducers."""
from studio.orchestrator.reducers import (
    register, get_reducer, list_reducers,
    majority_vote, concatenate, select_best_by, collect_all,
)


class TestRegistry:
    def test_all_builtin_registered(self):
        names = list_reducers()
        assert "majority_vote" in names
        assert "concatenate" in names
        assert "select_best_by" in names
        assert "collect_all" in names

    def test_get_reducer_returns_callable(self):
        fn = get_reducer("majority_vote")
        assert callable(fn)

    def test_get_reducer_missing(self):
        assert get_reducer("nonexistent") is None

    def test_custom_reducer_register(self):
        @register("test_custom")
        def custom(outputs, config):
            return sum(o.get("val", 0) for o in outputs)

        assert "test_custom" in list_reducers()
        result = custom([{"val": 1}, {"val": 2}], {})
        assert result == 3


class TestMajorityVote:
    def test_simple_majority(self):
        outputs = [{"answer": "A"}, {"answer": "B"}, {"answer": "A"}]
        assert majority_vote(outputs, {"field": "answer"}) == "A"

    def test_default_field(self):
        outputs = [{"answer": "yes"}, {"answer": "yes"}, {"answer": "no"}]
        assert majority_vote(outputs, {}) == "yes"

    def test_single_output(self):
        outputs = [{"answer": "foo"}]
        assert majority_vote(outputs, {}) == "foo"

    def test_empty_outputs(self):
        assert majority_vote([], {}) is None

    def test_missing_field(self):
        outputs = [{"x": 1}, {"x": 2}]
        assert majority_vote(outputs, {"field": "answer"}) is None

    def test_nested_field(self):
        outputs = [
            {"result": {"choice": "A"}},
            {"result": {"choice": "B"}},
            {"result": {"choice": "A"}},
        ]
        assert majority_vote(outputs, {"field": "result.choice"}) == "A"


class TestConcatenate:
    def test_strings_with_newline(self):
        outputs = [{"content": "a"}, {"content": "b"}]
        result = concatenate(outputs, {})
        assert result == "a\nb"

    def test_custom_separator(self):
        outputs = [{"content": "x"}, {"content": "y"}]
        result = concatenate(outputs, {"separator": ", "})
        assert result == "x, y"

    def test_list_parts(self):
        # Lists are extended, then strings joined with separator
        outputs = [{"content": ["a", "b"]}, {"content": ["c"]}]
        result = concatenate(outputs, {})
        assert result == "a\nb\nc"

    def test_mixed_types(self):
        # Non-strings converted via str(), then all joined
        outputs = [{"content": "hello"}, {"content": 42}]
        result = concatenate(outputs, {})
        assert result == "hello\n42"

    def test_empty_outputs(self):
        result = concatenate([], {})
        assert result == ""

    def test_custom_field(self):
        outputs = [{"text": "foo"}, {"text": "bar"}]
        result = concatenate(outputs, {"field": "text", "separator": "."})
        assert result == "foo.bar"


class TestSelectBestBy:
    def test_max_mode(self):
        outputs = [{"score": 0.5}, {"score": 0.9}, {"score": 0.3}]
        result = select_best_by(outputs, {})
        assert result == {"score": 0.9}

    def test_min_mode(self):
        outputs = [{"score": 0.5}, {"score": 0.9}, {"score": 0.3}]
        result = select_best_by(outputs, {"mode": "min"})
        assert result == {"score": 0.3}

    def test_nested_field(self):
        outputs = [
            {"metrics": {"accuracy": 0.8}},
            {"metrics": {"accuracy": 0.95}},
        ]
        result = select_best_by(outputs, {"field": "metrics.accuracy"})
        assert result == {"metrics": {"accuracy": 0.95}}

    def test_empty_outputs(self):
        assert select_best_by([], {}) is None

    def test_missing_field(self):
        outputs = [{"x": 1}, {"x": 2}]
        result = select_best_by(outputs, {"field": "score"})
        assert result is None


class TestCollectAll:
    def test_passthrough(self):
        outputs = [{"a": 1}, {"b": 2}]
        assert collect_all(outputs, {}) == outputs

    def test_empty(self):
        assert collect_all([], {}) == []
