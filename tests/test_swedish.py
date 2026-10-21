"""Ensure the English request wrapper does not force Swedish input into English."""
from app.chatbot import prompts


def test_swedish_question_not_forced_into_english():
    message = prompts.build_user_message("hej kan du hjälpa mej?", "English context", "en")
    assert "Answer in English." not in message
    assert "Swedish for Swedish questions" in message
    assert "hej kan du hjälpa mej?" in message


def test_language_capability_is_not_out_of_scope():
    assert "not off-topic" in prompts.SYSTEM_PROMPT
    assert "confirm briefly in Swedish" in prompts.SYSTEM_PROMPT


def test_arabic_wrapper_preserved():
    assert "أجب باللغة العربية الفصحى فقط." in prompts.build_user_message("مرحبا", "", "ar")
