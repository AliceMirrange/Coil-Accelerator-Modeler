"""Tests for native AEDT batch variable ChangeProperty payloads."""

from maxwell2d_toolkit.core import (
    _set_global_variables_low_level,
    _set_local_variables_low_level,
)


class FakeNativeHost:
    def __init__(self, variables=()):
        self.variables = list(variables)
        self.calls = []
        self.get_variables_calls = 0

    def GetVariables(self):
        self.get_variables_calls += 1
        return list(self.variables)

    def ChangeProperty(self, args):
        self.calls.append(args)
        tab = args[1]
        for section in tab[2:]:
            if section[0] == "NAME:NewProps":
                for prop in section[1:]:
                    name = prop[0][5:]
                    if name not in self.variables:
                        self.variables.append(name)
            elif section[0] == "NAME:ChangedProps":
                for prop in section[1:]:
                    name = prop[0][5:]
                    assert name in self.variables


class FakeApp:
    def __init__(self):
        self._oproject = FakeNativeHost(["$old"])
        self._odesign = FakeNativeHost(["old_local"])


def _sections(call):
    return {section[0]: section for section in call[1][2:]}


def test_global_batch_splits_new_and_existing_in_one_changeproperty_call():
    app = FakeApp()
    _set_global_variables_low_level(
        app,
        {"$old": "2mm", "$new_a": "3mm", "$new_b": "$new_a*2"},
    )
    assert len(app._oproject.calls) == 1
    assert app._oproject.get_variables_calls == 2  # once before, once after; independent of N
    call = app._oproject.calls[0]
    assert call[0] == "NAME:AllTabs"
    assert call[1][0] == "NAME:ProjectVariableTab"
    assert call[1][1] == ["NAME:PropServers", "ProjectVariables"]
    sections = _sections(call)
    assert ["NAME:$old", "Value:=", "2mm"] in sections["NAME:ChangedProps"]
    new_names = [row[0] for row in sections["NAME:NewProps"][1:]]
    assert new_names == ["NAME:$new_a", "NAME:$new_b"]
    for row in sections["NAME:NewProps"][1:]:
        assert row[1:5] == ["PropType:=", "VariableProp", "UserDef:=", True]


def test_local_batch_uses_local_variable_tab_and_can_add_variables():
    app = FakeApp()
    _set_local_variables_low_level(app, {"old_local": "2mm", "new_local": "4mm"})
    assert len(app._odesign.calls) == 1
    call = app._odesign.calls[0]
    assert call[1][0] == "NAME:LocalVariableTab"
    assert call[1][1] == ["NAME:PropServers", "LocalVariables"]
    sections = _sections(call)
    assert "NAME:NewProps" in sections
    assert "NAME:ChangedProps" in sections


def test_scope_name_validation():
    app = FakeApp()
    try:
        _set_global_variables_low_level(app, {"no_dollar": "1mm"})
    except ValueError:
        pass
    else:
        raise AssertionError("global variable without $ should fail")

    try:
        _set_local_variables_low_level(app, {"$wrong_scope": "1mm"})
    except ValueError:
        pass
    else:
        raise AssertionError("local variable with $ should fail")
