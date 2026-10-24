import os
from dotenv import load_dotenv
from litellm import completion

load_dotenv()

response = completion(
    model=os.getenv("LLM_MODEL"),
    messages=[{"role": "user", "content": "In one sentence, what does an SRE do?"}],
)

print(response.choices[0].message.content)
