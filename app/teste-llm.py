import asyncio
import json

import tools as tool_registry
from app.agent import SYSTEM
from llm import call_llm


async def main():
    response = await call_llm(
        system=SYSTEM,
        history=[],
        message="uber 12,50 crédito",
        tools=tool_registry.SCHEMAS,
    )
    print(json.dumps(response, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())