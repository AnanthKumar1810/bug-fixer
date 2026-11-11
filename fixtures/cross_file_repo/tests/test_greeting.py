from main import run
from service.formatter import format_greeting
from service.notifier import send_notification

def test_greeting():
    assert run() == 'Hello, World!'

def test_formatter_direct():
    assert format_greeting('Alice') == 'Hello, Alice!'

def test_second_caller():
    assert send_notification('Bob') == 'Hello, Bob!'
