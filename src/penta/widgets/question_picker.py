"""Modal screen for answering Claude's AskUserQuestion structured questions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, SelectionList
from textual.widgets.selection_list import Selection

_OTHER_LABEL = "Other"


class QuestionBlock(Vertical):
    """A single question with its options."""

    DEFAULT_CSS = """
    QuestionBlock {
        height: auto;
        margin: 0 0 1 0;
    }
    QuestionBlock .question-text {
        text-style: bold;
        margin: 0 0 1 0;
    }
    QuestionBlock .option-desc {
        color: $text-muted;
        margin: 0 0 0 4;
    }
    QuestionBlock .other-input {
        display: none;
        margin: 0 0 0 4;
    }
    QuestionBlock .other-input.visible {
        display: block;
    }
    """

    def __init__(
        self,
        question: str,
        options: list[dict],
        multi_select: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._question = question
        self._options = options
        self._multi_select = multi_select

    def compose(self) -> ComposeResult:
        yield Label(self._question, classes="question-text")

        if self._multi_select:
            selections = [
                Selection(opt["label"], opt["label"])
                for opt in self._options
            ]
            selections.append(Selection(_OTHER_LABEL, _OTHER_LABEL))
            yield SelectionList(*selections)
        else:
            with RadioSet():
                for opt in self._options:
                    yield RadioButton(opt["label"])
                yield RadioButton(_OTHER_LABEL)

        # Description hints below options
        for opt in self._options:
            desc = opt.get("description", "")
            if desc:
                yield Label(f"  {opt['label']}: {desc}", classes="option-desc")

        yield Input(placeholder="Type your answer...", classes="other-input")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        is_other = event.pressed.label.plain == _OTHER_LABEL
        inp = self.query_one(".other-input", Input)
        inp.set_class(is_other, "visible")
        if is_other:
            inp.focus()

    def on_selection_list_selection_toggled(
        self, event: SelectionList.SelectionToggled,
    ) -> None:
        sel_list = self.query_one(SelectionList)
        selected_values = set(sel_list.selected)
        is_other = _OTHER_LABEL in selected_values
        inp = self.query_one(".other-input", Input)
        inp.set_class(is_other, "visible")

    def get_answer(self) -> str:
        """Return the selected answer(s) as a string."""
        if self._multi_select:
            sel_list = self.query_one(SelectionList)
            selected = set(sel_list.selected)
            if _OTHER_LABEL in selected:
                selected.discard(_OTHER_LABEL)
                other_text = self.query_one(".other-input", Input).value.strip()
                if other_text:
                    selected.add(other_text)
            return ", ".join(sorted(selected))
        else:
            radio_set = self.query_one(RadioSet)
            if radio_set.pressed_button is None:
                return ""
            label = radio_set.pressed_button.label.plain
            if label == _OTHER_LABEL:
                return self.query_one(".other-input", Input).value.strip()
            return label


class QuestionPickerScreen(ModalScreen[dict[str, str] | None]):
    """Modal overlay presenting Claude's structured questions."""

    DEFAULT_CSS = """
    QuestionPickerScreen {
        align: center middle;
    }
    QuestionPickerScreen #dialog {
        width: 70%;
        max-height: 80%;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    QuestionPickerScreen #dialog-title {
        text-style: bold;
        margin: 0 0 1 0;
    }
    QuestionPickerScreen #button-bar {
        height: auto;
        margin: 1 0 0 0;
        align: right middle;
    }
    QuestionPickerScreen #button-bar Button {
        margin: 0 0 0 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        agent_name: str,
        questions: list[dict],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._agent_name = agent_name
        self._questions = questions

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(
                f"{self._agent_name} has a question:",
                id="dialog-title",
            )
            for i, q in enumerate(self._questions):
                yield QuestionBlock(
                    question=q.get("question", ""),
                    options=q.get("options", []),
                    multi_select=q.get("multiSelect", False),
                    id=f"question-{i}",
                )
            with Horizontal(id="button-bar"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Submit", variant="primary", id="submit-btn")

    def on_mount(self) -> None:
        # Focus the first question's option set
        blocks = self.query(QuestionBlock)
        if blocks:
            radio = blocks.first().query(RadioSet)
            sel = blocks.first().query(SelectionList)
            if radio:
                radio.first().focus()
            elif sel:
                sel.first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit-btn":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        answers: dict[str, str] = {}
        for i, q in enumerate(self._questions):
            block = self.query_one(f"#question-{i}", QuestionBlock)
            answer = block.get_answer()
            if not answer:
                self.notify("Please answer all questions before submitting", severity="warning")
                return
            answers[q.get("question", "")] = answer
        self.dismiss(answers)
