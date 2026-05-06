# test_gemini_tools.py
import os
import asyncio
from google import genai
from google.genai import types

API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=API_KEY)

async def main():
    tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="add_numbers",
                    description="Soma dois números inteiros.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "a": types.Schema(type="INTEGER", description="Primeiro número."),
                            "b": types.Schema(type="INTEGER", description="Segundo número."),
                        },
                        required=["a", "b"],
                    ),
                )
            ]
        )
    ]

    chat = client.aio.chats.create(
        model="gemini-2.5-flash-lite",
        config=types.GenerateContentConfig(
            system_instruction=types.Content(
                parts=[types.Part(text=(
                    "Você é um assistente de teste. "
                    "Sempre que eu pedir para somar números, use SEMPRE a função add_numbers."
                ))]
            ),
            tools=tools,
        ),
    )

    response = await chat.send_message("Por favor, some 2 e 3 usando a função disponível.")

    candidate = response.candidates[0]
    print("response.text:", response.text)

    for i, part in enumerate(candidate.content.parts):
        print(f"Part {i}:", part)
        if getattr(part, "function_call", None):
            fc = part.function_call
            print("FOUND FUNCTION CALL!")
            print("name:", fc.name)
            print("args:", fc.args)

if __name__ == "__main__":
    asyncio.run(main())