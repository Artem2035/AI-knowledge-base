from config.settings import Settings
from llm.groq_client import GroqClient

settings = Settings()

print("model:", settings.groq_model)
print("timeout:", settings.groq_timeout_seconds)
print("api key exists:", bool(settings.groq_api_key))

# Проверяем тот же OpenAI-клиент, который создаёт GroqClient,
# но не вызываем generate_structured и не создаём budget.

from openai import OpenAI

client = OpenAI(
    api_key=settings.groq_api_key,
    base_url="https://api.groq.com/openai/v1",
    timeout=60.0,
    max_retries=0,
)

result = client.chat.completions.create(
    model=settings.groq_model,
    messages=[
        {
            "role": "system",
            "content": "Отвечай строго в формате JSON. Например: {\"response\": \"привет\"}",
        },
        {
            "role": "user",
            "content": "Ответь одним словом: привет. Оберни ответ в JSON-ключ 'response'.",
        },
    ],
    response_format={"type": "json_object"},
)


print(result.choices[0].message.content)