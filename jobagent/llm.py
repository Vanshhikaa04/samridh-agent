from openai import OpenAI

from .config import env

_client = None


def openai_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=env("OPENAI_API_KEY"))
    return _client


def chat(system: str, user: str, json_mode: bool = False) -> str:
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = openai_client().chat.completions.create(
        model=env("OPENAI_CHAT_MODEL"), temperature=0.3, **kw,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return r.choices[0].message.content


def embed(texts: list[str]) -> list[list[float]]:
    r = openai_client().embeddings.create(model=env("OPENAI_EMBED_MODEL"), input=texts)
    return [d.embedding for d in r.data]
