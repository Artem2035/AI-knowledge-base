from pydantic import BaseModel

from llm.chunking import split_items_into_batches


class _Out(BaseModel):
    value: str


class _Client:
    def available_prompt_budget_tokens(self, system_instruction, response_model):
        return 10

    def estimate_tokens(self, text):
        return len(text)  # 1 символ = 1 токен


def test_batches_use_client_token_counter_for_items():
    batches = split_items_into_batches(
        ["aaaa", "bbbb", "cccc"], client=_Client(), system_instruction="s",
        response_model=_Out, render_item=lambda x: x,
    )
    assert batches == [["aaaa", "bbbb"], ["cccc"]]