import asyncio
import sys
from pluggy_service import PluggyService

async def main():
    # Permite passar o nome do arquivo como argumento: python analisar_pluggy.py outro_teste.json
    arquivo = sys.argv[1] if len(sys.argv) > 1 else "transacoes.json"
    telefone = "whatsapp:+5511976582394" # Substitua pelo seu telefone cadastrado

    service = PluggyService()
    print(f"--- Iniciando importação de: {arquivo} ---")
    resultado = service.sync_from_file(telefone, arquivo)
    print(f"\nResultado:\n{resultado}")

if __name__ == "__main__":
    asyncio.run(main())
