import asyncio
from pluggy_service import PluggyService

async def main():
    service = PluggyService()
    # Substitua pelo caminho do seu arquivo e o telefone de teste
    resultado = service.sync_from_file("whatsapp:+5511976582394", "transacoes.json")
    print(resultado)

if __name__ == "__main__":
    asyncio.run(main())
