from services.agent_runtime.message_content import message_content_to_text


def test_null_text_blocks_are_ignored_instead_of_rendered_as_none():
    assert message_content_to_text([{"type": "text", "text": None}, {"text": None}]) == ""


def test_mixed_multimodal_content_keeps_only_real_text_blocks():
    content = [
        {"type": "image", "image_url": "data:image/png;base64,..."},
        {"type": "text", "text": "Your spending was "},
        {"type": "text", "text": None},
        {"type": "text", "text": "EUR 4.85."},
        42,
    ]
    assert message_content_to_text(content) == "Your spending was EUR 4.85."


def test_plain_string_content_is_unchanged():
    assert message_content_to_text("Hello") == "Hello"
    assert message_content_to_text(None) == ""
