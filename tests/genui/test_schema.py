"""schema — official SDK catalog/validator over the vendored mirror, and
the prompt summary carrying the CORRECT v0.9 binding syntax."""
from __future__ import annotations

from taskvm.genui import protocol, schema


def test_catalog_id_equals_mirror_dollar_id():
    assert schema.get_catalog().catalog_id == protocol.CATALOG_ID


def test_basic_catalog_has_the_18_components():
    names = schema.basic_catalog_names()
    assert len(names) == 18
    for expected in ("Text", "Row", "Column", "Card", "List", "Tabs",
                     "Divider", "Button", "TextField", "CheckBox",
                     "ChoicePicker", "Slider", "DateTimeInput", "Image",
                     "Icon", "Video", "AudioPlayer", "Modal"):
        assert expected in names, expected


def test_validator_accepts_conformant_stream():
    sid = "taskvm-task-schema-test"
    stream = [
        protocol.create_surface_message(sid),
        protocol.update_components_message(sid, [
            {"id": "root", "component": "Column", "children": ["f"]},
            {"id": "f", "component": "TextField", "label": "x",
             "value": {"path": "/variables/k/desired"}},
        ]),
        protocol.update_data_model_message(sid, {"variables": {"k": {}}}),
    ]
    assert schema.validate_protocol_messages(stream) == []


def test_validator_rejects_unknown_component():
    errors = schema.validate_protocol_messages([{
        "version": protocol.PROTOCOL_VERSION,
        "updateComponents": {
            "surfaceId": "s", "components": [
                {"id": "root", "component": "NotAComponent"}]}}])
    assert errors and "NotAComponent" in errors[0]


def test_validator_rejects_v08_data_binding_property():
    errors = schema.validate_protocol_messages([{
        "version": protocol.PROTOCOL_VERSION,
        "updateComponents": {
            "surfaceId": "s", "components": [
                {"id": "root", "component": "Column", "children": ["f"]},
                {"id": "f", "component": "TextField", "label": "x",
                 "dataBinding": "/variables/k/desired"}]}}])
    assert errors and "dataBinding" in " ".join(errors)


def test_prompt_summary_uses_path_binding_not_legacy_data_binding():
    summary = schema.catalog_prompt_summary()
    assert '{"path"' in summary                       # v0.9 DataBinding form
    assert "dataBinding" in summary                   # …mentioned only as banned
    assert "NO \"dataBinding\"" in summary
    assert "taskvm.local_patch" in summary


def test_prompt_summary_lists_all_18_components():
    summary = schema.catalog_prompt_summary()
    for name in schema.basic_catalog_names():
        assert name in summary
