import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_infoflow


class FakeJournal:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


def _record(**overrides):
    record = {
        "decision_id": "398ce03ee58ea78f",
        "run_id": "run-1",
        "group_id": "13403269",
        "asked_at": "2026-09-03T03:39:20.740482+00:00",
        "options": [{"key": "continue", "text": "继续"}, {"key": "hold", "text": "停"}],
    }
    record.update(overrides)
    return record


def _message(**overrides):
    message = {
        "message_id": "1",
        "sender": "zhaotao02",
        "text": "CHOICE 398ce03ee58ea78f continue",
        "received_at": "2026-09-03T03:39:45.548Z",
        "chat_type": "group",
        "group_id": "13403269",
    }
    message.update(overrides)
    return message


class AnswerTests(unittest.TestCase):
    def test_a_tap_in_the_runs_group_after_the_question_is_the_answer(self):
        answer = ask_infoflow._answer(_record(), FakeJournal([_message()]))
        self.assertEqual(answer["option"], "continue")
        self.assertEqual(answer["responder"], "zhaotao02")

    def test_a_tap_from_another_group_is_not_the_answer(self):
        # The question was asked in one group; the same words elsewhere decide nothing.
        journal = FakeJournal([_message(group_id="999")])
        self.assertIsNone(ask_infoflow._answer(_record(), journal))

    def test_a_line_older_than_the_question_is_not_the_answer(self):
        journal = FakeJournal([_message(received_at="2026-09-03T03:00:00.000Z")])
        self.assertIsNone(ask_infoflow._answer(_record(), journal))

    def test_another_decisions_answer_is_ignored(self):
        journal = FakeJournal([_message(text="CHOICE 0000000000000000 continue")])
        self.assertIsNone(ask_infoflow._answer(_record(), journal))

    def test_an_option_the_question_never_offered_is_ignored(self):
        journal = FakeJournal([_message(text="CHOICE 398ce03ee58ea78f drop")])
        self.assertIsNone(ask_infoflow._answer(_record(), journal))

    def test_a_private_reply_is_not_read_as_a_choice(self):
        # Only the group the card was sent to scopes a non-gate answer.
        journal = FakeJournal([_message(chat_type="single", group_id=None)])
        self.assertIsNone(ask_infoflow._answer(_record(), journal))


class WaitTests(unittest.TestCase):
    def test_waiting_on_an_unknown_decision_is_rejected(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        original = ask_infoflow._HOME
        ask_infoflow._HOME = Path(directory.name)
        self.addCleanup(lambda: setattr(ask_infoflow, "_HOME", original))
        with self.assertRaises(SystemExit):
            ask_infoflow.main(["wait", "run-1", "0000000000000000", "--timeout", "0"])

    def test_a_question_nobody_answers_times_out_instead_of_deciding(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        original = ask_infoflow._HOME
        ask_infoflow._HOME = Path(directory.name)
        self.addCleanup(lambda: setattr(ask_infoflow, "_HOME", original))
        # An empty journal, not the machine's real one, decides this test.
        journal = Path(directory.name) / "empty.jsonl"
        journal.write_text("", encoding="utf-8")
        previous = os.environ.get("TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL")
        os.environ["TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL"] = str(journal)
        self.addCleanup(
            lambda: os.environ.pop("TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL")
            if previous is None
            else os.environ.__setitem__("TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL", previous)
        )
        path = ask_infoflow._record_path("398ce03ee58ea78f")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_record()), encoding="utf-8")

        class Args:
            decision_id = "398ce03ee58ea78f"
            timeout = 0.0
            poll = 0.0

        result = ask_infoflow.wait(Args())
        self.assertEqual(result["status"], "TIMEOUT")


if __name__ == "__main__":
    unittest.main()
