from homepilot.artifacts.validators import (
    validate_artifact_expressions,
    validate_jinja2_template,
    validate_skip_if,
)


class TestValidateJinja2Valid:
    def test_plain_text(self):
        assert validate_jinja2_template("Hello world") == []

    def test_simple_variable(self):
        assert validate_jinja2_template("{{ target }}") == []

    def test_host_variable(self):
        assert validate_jinja2_template("{{ host }}") == []

    def test_inventory_loop(self):
        assert validate_jinja2_template("{% for item in inventory %}{{ item }}{% endfor %}") == []

    def test_vault_access(self):
        assert validate_jinja2_template("{{ vault.key }}") == []

    def test_fact_variable(self):
        assert validate_jinja2_template("{{ fact }}") == []

    def test_conditional(self):
        assert validate_jinja2_template("{% if target %}yes{% endif %}") == []


class TestValidateJinja2Invalid:
    def test_syntax_error(self):
        errors = validate_jinja2_template("{% if target %}")
        assert len(errors) == 1
        assert "syntax error" in errors[0].lower()

    def test_undefined_variable(self):
        errors = validate_jinja2_template("{{ nonexistent_var }}")
        assert len(errors) >= 1
        assert "undefined" in errors[0].lower()

    def test_dangerous_sandbox_escape(self):
        errors = validate_jinja2_template("{{ ''.__class__.__mro__ }}")
        assert len(errors) >= 1

    def test_dangerous_class_access(self):
        errors = validate_jinja2_template("{{ [].__class__ }}")
        assert len(errors) >= 1


class TestValidateSkipIfValid:
    def test_simple_comparison(self):
        assert validate_skip_if("status == 'applied'") == []

    def test_not_expression(self):
        assert validate_skip_if("not status") == []

    def test_and_expression(self):
        assert validate_skip_if("status == 'applied' and kind == 'shell-script'") == []

    def test_or_expression(self):
        assert validate_skip_if("status == 'applied' or kind == 'kb-note'") == []

    def test_comparison_with_int(self):
        assert validate_skip_if("fact == 42") == []

    def test_none_comparison(self):
        assert validate_skip_if("target is None") == []

    def test_bool_literals(self):
        assert validate_skip_if("True and not False") == []

    def test_allowlisted_builtin_len(self):
        assert validate_skip_if("len(inventory)") == []

    def test_allowlisted_builtin_str(self):
        assert validate_skip_if("str(fact)") == []

    def test_string_key_subscript(self):
        assert validate_skip_if("vars['key'] == 'value'") == []


class TestValidateSkipIfInvalid:
    def test_import_blocked(self):
        errors = validate_skip_if("import os")
        assert len(errors) >= 1

    def test_function_call_blocked(self):
        errors = validate_skip_if("print('hello')")
        assert any("not allowed" in e.lower() for e in errors)

    def test_attribute_access_blocked(self):
        errors = validate_skip_if("target.host")
        assert any("attribute" in e.lower() for e in errors)

    def test_comprehension_blocked(self):
        errors = validate_skip_if("[x for x in inventory]")
        assert any("comprehension" in e.lower() for e in errors)

    def test_lambda_blocked(self):
        errors = validate_skip_if("lambda x: x")
        assert any("lambda" in e.lower() for e in errors)

    def test_walrus_blocked(self):
        errors = validate_skip_if("(x := 5)")
        assert any("walrus" in e.lower() for e in errors)

    def test_non_string_subscript_blocked(self):
        errors = validate_skip_if("inventory[0]")
        assert any("subscript" in e.lower() for e in errors)

    def test_non_allowlisted_var_blocked(self):
        errors = validate_skip_if("unknown_var == 1")
        assert any("not allowlisted" in e.lower() for e in errors)

    def test_dunder_blocked(self):
        errors = validate_skip_if("__class__")
        assert any("dunder" in e.lower() for e in errors)


class TestValidateArtifactExpressions:
    def test_no_expressions_valid(self):
        fm = {"kind": "shell-script"}
        body = "just a plain body"
        assert validate_artifact_expressions(fm, body) == []

    def test_skip_if_valid(self):
        fm = {"skip_if": "status == 'applied'"}
        body = "body"
        assert validate_artifact_expressions(fm, body) == []

    def test_skip_if_invalid(self):
        fm = {"skip_if": "target.host"}
        body = "body"
        errors = validate_artifact_expressions(fm, body)
        assert len(errors) >= 1

    def test_body_jinja2_valid(self):
        fm = {"kind": "shell-script"}
        body = "{{ target }}"
        assert validate_artifact_expressions(fm, body) == []

    def test_body_jinja2_invalid(self):
        fm = {"kind": "shell-script"}
        body = "{% if target %}"
        errors = validate_artifact_expressions(fm, body)
        assert len(errors) >= 1

    def test_both_skip_if_and_jinja2_errors(self):
        fm = {"skip_if": "target.host"}
        body = "{% if target %}"
        errors = validate_artifact_expressions(fm, body)
        assert len(errors) >= 2

    def test_tag_in_body_triggers_validation(self):
        fm = {"kind": "shell-script"}
        body = "{% for h in inventory %}{{ h }}{% endfor %}"
        assert validate_artifact_expressions(fm, body) == []
