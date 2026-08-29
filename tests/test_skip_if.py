import pytest

from homepilot.executor.skip_if import (
    SkipIfUndecided,
    _ResponseProxy,
    make_response_proxy,
    safe_eval_skip_if,
)

# Every refusal below asserted `is False`. False is the branch that RUNS the
# mutating step, so an expression the evaluator would not run was silently
# upgraded to "yes, apply it" - in the one guard whose job is to stop that
# (#642, review #648). The refusals are unchanged; what they answer is not.


class TestSafeEvalAllowedExpressions:
    def test_comparison_eq(self):
        r = type("R", (), {"status": 200})()
        assert safe_eval_skip_if("response.status == 200", r, {}) is True

    def test_comparison_ne(self):
        r = type("R", (), {"status": 200})()
        assert safe_eval_skip_if("response.status != 404", r, {}) is True

    def test_comparison_lt(self):
        r = type("R", (), {"code": 3})()
        assert safe_eval_skip_if("response.code < 5", r, {}) is True

    def test_comparison_gt(self):
        r = type("R", (), {"code": 3})()
        assert safe_eval_skip_if("response.code > 0", r, {}) is True

    def test_comparison_in(self):
        r = type("R", (), {"status": 200})()
        assert safe_eval_skip_if("response.status in [200, 201]", r, {}) is True

    def test_bool_and(self):
        r = type("R", (), {"status": 200, "ok": True})()
        assert safe_eval_skip_if("response.status == 200 and response.ok == True", r, {}) is True

    def test_bool_or(self):
        r = type("R", (), {"status": 404, "ok": False})()
        assert safe_eval_skip_if("response.status == 200 or response.ok == False", r, {}) is True

    def test_not(self):
        assert safe_eval_skip_if("not True", None, {}) is False

    def test_subscript_access(self):
        ctx_obj = {"key": "val"}
        assert safe_eval_skip_if("target['key'] == 'val'", None, ctx_obj) is True

    def test_attribute_access(self):
        r = type("R", (), {"status": 200})()
        assert safe_eval_skip_if("response.status == 200", r, {}) is True

    def test_name_constants(self):
        assert safe_eval_skip_if("True", None, {}) is True
        assert safe_eval_skip_if("False", None, {}) is False

    def test_any_generator_known_issue(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("any(x > 0 for x in [1, 2, 3])", None, {})

    def test_all_generator_known_issue(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("all(x > 0 for x in [1, 2, 3])", None, {})


class TestDunderAttributeBlocked:
    def test_class_dunder_blocked(self):
        r = type("R", (), {"status": 200})()
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response.__class__", r, {})

    def test_dict_dunder_blocked(self):
        r = type("R", (), {"status": 200})()
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response.__dict__", r, {})

    def test_private_attr_blocked(self):
        r = type("R", (), {"_secret": "hidden"})()
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response._secret == 'hidden'", r, {})

    def test_subclasses_chain_blocked(self):
        r = type("R", (), {})()
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response.__class__.__bases__", r, {})


class TestSafeEvalBlockedCalls:
    def test_import_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("__import__('os')", None, {})

    def test_eval_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("eval('1')", None, {})

    def test_exec_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("exec('pass')", None, {})

    def test_arbitrary_function_call_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("print('hi')", None, {})

    def test_os_system_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("os.system('ls')", None, {})

    def test_os_module_access(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("os", None, {})

    def test_unknown_name_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("unknown_var == 1", None, {})

    def test_complex_expression_blocked(self):
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("[x for x in range(10)]", None, {})


class TestComparisonOps:
    def test_le(self):
        r = type("R", (), {"v": 5})()
        assert safe_eval_skip_if("response.v <= 5", r, {}) is True
        assert safe_eval_skip_if("response.v <= 4", r, {}) is False

    def test_ge(self):
        r = type("R", (), {"v": 5})()
        assert safe_eval_skip_if("response.v >= 5", r, {}) is True
        assert safe_eval_skip_if("response.v >= 6", r, {}) is False

    def test_is_isnot(self):
        assert safe_eval_skip_if("True is True", None, {}) is True
        assert safe_eval_skip_if("True is not True", None, {}) is False

    def test_not_in(self):
        r = type("R", (), {"status": 200})()
        assert safe_eval_skip_if("response.status not in [404, 500]", r, {}) is True


class TestBooleanOps:
    def test_and_short_circuit(self):
        r = type("R", (), {"a": 0, "b": 1})()
        assert safe_eval_skip_if("response.a and response.b", r, {}) is False

    def test_or_short_circuit(self):
        r = type("R", (), {"a": 1, "b": 0})()
        assert safe_eval_skip_if("response.a or response.b", r, {}) is True

    def test_nested_boolean(self):
        r = type("R", (), {"x": 1, "y": 2, "z": 3})()
        expr = "(response.x == 1 or response.y == 5) and response.z == 3"
        assert safe_eval_skip_if(expr, r, {}) is True


class TestResponseProxy:
    def test_make_response_proxy_from_httpx_like(self):
        class FakeResp:
            status_code = 200

            def __init__(self):
                self.headers = {"content-type": "application/json", "authorization": "Bearer tok"}

            def json(self):
                return {"status": "running"}

        proxy = make_response_proxy(FakeResp())
        assert proxy.status_code == 200
        assert "content-type" in proxy.headers
        assert "authorization" not in proxy.headers
        assert proxy.json == {"status": "running"}

    def test_make_response_proxy_excludes_cookie_headers(self):
        class FakeResp:
            status_code = 201

            def __init__(self):
                self.headers = {"set-cookie": "sess=abc", "x-request-id": "xyz"}

            def json(self):
                return {}

        proxy = make_response_proxy(FakeResp())
        assert "set-cookie" not in proxy.headers
        assert "x-request-id" in proxy.headers

    def test_make_response_proxy_idempotent(self):
        proxy = _ResponseProxy(200, {"content-type": "text/plain"}, {"ok": True})
        assert make_response_proxy(proxy) is proxy

    def test_proxy_prevents_request_header_access(self):
        class FakeResp:
            status_code = 200

            def __init__(self):
                self.headers = {}

            request = type("Req", (), {"headers": {"authorization": "Bearer tok"}})()

            def json(self):
                return {}

        proxy = make_response_proxy(FakeResp())
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response.request", proxy, {})

    def test_proxy_prevents_private_attr_access(self):
        proxy = _ResponseProxy(200, {}, None)
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response._private", proxy, {})

    def test_proxy_status_code_eval(self):
        proxy = _ResponseProxy(200, {}, None)
        assert safe_eval_skip_if("response.status_code == 200", proxy, {}) is True
        assert safe_eval_skip_if("response.status_code == 404", proxy, {}) is False

    def test_proxy_json_subscript(self):
        proxy = _ResponseProxy(200, {}, {"status": "running", "count": 3})
        assert safe_eval_skip_if("response.json['status'] == 'running'", proxy, {}) is True
        assert safe_eval_skip_if("response.json['count'] > 0", proxy, {}) is True

    def test_proxy_headers_subscript(self):
        proxy = _ResponseProxy(200, {"content-type": "application/json"}, None)
        assert (
            safe_eval_skip_if("response.headers['content-type'] == 'application/json'", proxy, {})
            is True
        )

    def test_proxy_json_none_safe(self):
        proxy = _ResponseProxy(200, {}, None)
        assert safe_eval_skip_if("response.json == None", proxy, {}) is True
