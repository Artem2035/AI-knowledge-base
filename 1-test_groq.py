# from config.settings import Settings
# from llm.groq_client import GroqClient
#
# settings = Settings()
#
# print("model:", settings.groq_model)
# print("timeout:", settings.groq_timeout_seconds)
# print("api key exists:", bool(settings.groq_api_key))
#
# # Проверяем тот же OpenAI-клиент, который создаёт GroqClient,
# # но не вызываем generate_structured и не создаём budget.
#
# from openai import OpenAI
#
# client = OpenAI(
#     api_key=settings.groq_api_key,
#     base_url="https://api.groq.com/openai/v1",
#     timeout=60.0,
#     max_retries=0,
# )
#
# result = client.chat.completions.create(
#     model=settings.groq_model,
#     messages=[
#         {
#             "role": "system",
#             "content": "Отвечай строго в формате JSON. Например: {\"response\": \"привет\"}",
#         },
#         {
#             "role": "user",
#             "content": "Ответь одним словом: привет. Оберни ответ в JSON-ключ 'response'.",
#         },
#     ],
#     response_format={"type": "json_object"},
# )
#
#
# print(result.choices[0].message.content)
import os
import httpx, socket, time
def c():
    print({k: v for k, v in os.environ.items() if 'proxy' in k.lower()})

def a():
    t=time.time()
    try:
        r = httpx.get('https://html.duckduckgo.com/html/?q=test', timeout=10)
        print('OK', r.status_code, time.time()-t)
    except Exception as e:
        print('FAIL', type(e).__name__, e, time.time()-t)
c()
def b():
    orig = socket.getaddrinfo
    def ipv4_only(*args, **kwargs):
        return [r for r in orig(*args, **kwargs) if r[0] == socket.AF_INET]
    socket.getaddrinfo = ipv4_only
    t=time.time()
    try:
        r = httpx.get('https://html.duckduckgo.com/html/?q=test', timeout=10)
        print('OK via IPv4', r.status_code, time.time()-t)
    except Exception as e:
        print('FAIL via IPv4', type(e).__name__, e, time.time()-t)