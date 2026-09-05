from pydantic import BaseModel
from config.settings import Settings
from llm.groq_client import GroqClient


class FakeBudget:
    def check_and_register_task_call(self, status):
        pass

    def check_rpd_soft_limit(self):
        pass

    def wait_if_needed_for_rpm(self):
        pass

    def register_call(self, status, role, ok, error=None):
        pass


class Answer(BaseModel):
    answer: str


settings = Settings()

client = GroqClient(
    settings=settings,
    budget=FakeBudget(),
)

result = client.generate_structured(
    role="test",
    prompt="Ответь одним словом: привет",
    response_model=Answer,
    status=None,
    system_instruction="Отвечай кратко.",
)

print(result)
print(result.model_dump())